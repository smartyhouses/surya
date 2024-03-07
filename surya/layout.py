import math
from collections import defaultdict
from typing import List, Optional
from PIL import Image
import numpy as np

from surya.detection import batch_detection
from surya.postprocessing.heatmap import keep_largest_boxes, get_and_clean_boxes, get_detected_boxes, \
    clean_contained_boxes
from surya.schema import LayoutResult, LayoutBox, TextDetectionResult


def compute_integral_image(arr):
    return arr.cumsum(axis=0).cumsum(axis=1)


def bbox_avg(integral_image, x1, y1, x2, y2):
    total = integral_image[y2, x2]
    above = integral_image[y1 - 1, x2] if y1 > 0 else 0
    left = integral_image[y2, x1 - 1] if x1 > 0 else 0
    above_left = integral_image[y1 - 1, x1 - 1] if (x1 > 0 and y1 > 0) else 0
    bbox_sum = total - above - left + above_left
    bbox_area = (x2 - x1) * (y2 - y1)
    if bbox_area == 0:
        return 0
    return bbox_sum / bbox_area


def get_regions_from_detection_result(detection_result: TextDetectionResult, heatmaps: List[Image.Image], orig_size, id2label, segment_assignment, vertical_line_width=20) -> List[LayoutBox]:
    logits = np.stack(heatmaps, axis=0)
    vertical_line_bboxes = [line for line in detection_result.vertical_lines]
    line_bboxes = detection_result.bboxes

    # Scale back to processor size
    for line in vertical_line_bboxes:
        line.rescale_bbox(orig_size, list(reversed(heatmaps[0].shape)))

    for line in line_bboxes:
        line.rescale(orig_size, list(reversed(heatmaps[0].shape)))

    for bbox in vertical_line_bboxes:
        # Give some width to the vertical lines
        vert_bbox = list(bbox.bbox)
        vert_bbox[2] = min(heatmaps[0].shape[0], vert_bbox[2] + vertical_line_width)

        logits[:, vert_bbox[1]:vert_bbox[3], vert_bbox[0]:vert_bbox[2]] = 0  # zero out where the column lines are

    logits[:, logits[0] >= .5] = 0 # zero out where blanks are

    # Zero out where other segments are
    for i in range(logits.shape[0]):
        logits[i, segment_assignment != i] = 0

    detected_boxes = []
    for heatmap_idx in range(1, len(id2label)):  # Skip the blank class
        heatmap = logits[heatmap_idx]
        bboxes = get_detected_boxes(heatmap, text_threshold=.9, low_text=.8)
        bboxes = [bbox for bbox in bboxes if bbox.area > 25]
        for bb in bboxes:
            bb.fit_to_bounds([0, 0, heatmap.shape[1] - 1, heatmap.shape[0] - 1])

        integral_image = compute_integral_image(heatmap)
        bbox_confidences = [bbox_avg(integral_image, *[int(b) for b in bbox.bbox]) for bbox in bboxes]
        for confidence, bbox in zip(bbox_confidences, bboxes):
            if confidence <= .3:
                continue
            bbox = LayoutBox(polygon=bbox.polygon, label=id2label[heatmap_idx], confidence=confidence)
            detected_boxes.append(bbox)

    detected_boxes = sorted(detected_boxes, key=lambda x: x.confidence, reverse=True)
    # Expand bbox to cover intersecting lines
    box_lines = defaultdict(list)
    used_lines = set()
    for bbox_idx, bbox in enumerate(detected_boxes):
        for line_idx, line_bbox in enumerate(line_bboxes):
            if line_bbox.intersection_pct(bbox) >= .5 and line_idx not in used_lines:
                box_lines[bbox_idx].append(line_bbox.bbox)
                used_lines.add(line_idx)

    new_boxes = []
    for bbox_idx, bbox in enumerate(detected_boxes):
        if bbox_idx not in box_lines and bbox.label not in ["Picture", "Formula"]:
            continue

        if bbox_idx in box_lines and bbox.label in ["Picture"]:
            bbox.label = "Figure"

        covered_lines = box_lines[bbox_idx]
        if len(covered_lines) > 0 and bbox.label not in ["Picture"]:
            min_x = min([line[0] for line in covered_lines])
            min_y = min([line[1] for line in covered_lines])
            max_x = max([line[2] for line in covered_lines])
            max_y = max([line[3] for line in covered_lines])

            if bbox.label in ["Figure", "Table", "Formula"]:
                # Figures can tables can contain text, but text isn't the whole area
                min_x_box = min([b[0] for b in bbox.polygon])
                min_y_box = min([b[1] for b in bbox.polygon])
                max_x_box = max([b[0] for b in bbox.polygon])
                max_y_box = max([b[1] for b in bbox.polygon])

                min_x = min(min_x, min_x_box)
                min_y = min(min_y, min_y_box)
                max_x = max(max_x, max_x_box)
                max_y = max(max_y, max_y_box)

            bbox.polygon[0][0] = min_x
            bbox.polygon[0][1] = min_y
            bbox.polygon[1][0] = max_x
            bbox.polygon[1][1] = min_y
            bbox.polygon[2][0] = max_x
            bbox.polygon[2][1] = max_y
            bbox.polygon[3][0] = min_x
            bbox.polygon[3][1] = max_y

        new_boxes.append(bbox)

    unused_lines = [line for idx, line in enumerate(line_bboxes) if idx not in used_lines]
    for bbox in unused_lines:
        new_boxes.append(LayoutBox(polygon=bbox.polygon, label="Text", confidence=.5))

    for bbox in new_boxes:
        bbox.rescale(list(reversed(heatmap.shape)), orig_size)

    detected_boxes = [bbox for bbox in new_boxes if bbox.area > 16]
    return detected_boxes


def get_regions(heatmaps: List[Image.Image], orig_size, id2label, segment_assignment) -> List[LayoutBox]:
    bboxes = []
    for i in range(1, len(id2label)):  # Skip the blank class
        heatmap = heatmaps[i]
        assert heatmap.shape == segment_assignment.shape
        heatmap[segment_assignment != i] = 0  # zero out where another segment is
        bbox = get_and_clean_boxes(heatmap, list(reversed(heatmap.shape)), orig_size, low_text=.7, text_threshold=.8)
        for bb in bbox:
            bboxes.append(LayoutBox(polygon=bb.polygon, label=id2label[i]))
        heatmaps.append(heatmap)

    bboxes = keep_largest_boxes(bboxes)
    return bboxes


def batch_layout_detection(images: List, model, processor, detection_results: Optional[List[TextDetectionResult]] = None) -> List[LayoutResult]:
    preds, orig_sizes = batch_detection(images, model, processor)
    id2label = model.config.id2label

    results = []
    for i in range(len(images)):
        heatmaps = preds[i]
        orig_size = orig_sizes[i]
        logits = np.stack(heatmaps, axis=0)
        segment_assignment = logits.argmax(axis=0)

        if detection_results:
            bboxes = get_regions_from_detection_result(detection_results[i], heatmaps, orig_size, id2label, segment_assignment)
        else:
            bboxes = get_regions(heatmaps, orig_size, id2label, segment_assignment)

        segmentation_img = Image.fromarray(segment_assignment.astype(np.uint8))

        result = LayoutResult(
            bboxes=bboxes,
            segmentation_map=segmentation_img,
            heatmaps=heatmaps,
            image_bbox=[0, 0, orig_size[0], orig_size[1]]
        )

        results.append(result)

    return results