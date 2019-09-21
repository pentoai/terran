import numpy as np
import os
import torch

from terran import default_device
from terran.face.detection.retinaface.utils.bbox_transform import clip_boxes
from terran.face.detection.retinaface.utils.generate_anchor import (
    generate_anchors_fpn, anchors_plane,
)
from terran.face.detection.retinaface.utils.nms import (
    gpu_nms_wrapper, cpu_nms_wrapper,
)
from terran.face.detection.retinaface.model import (
    RetinaFace as RetinaFaceModel
)


def load_model():
    model = RetinaFaceModel()
    model.load_state_dict(torch.load(
        os.path.expanduser('~/.terran/checkpoints/retinaface-mnet.pth')
    ))
    model.eval()
    return model


class RetinaFace:

    def __init__(self, device=default_device, ctx_id=0, nms_threshold=0.4):
        self.device = device
        self.ctx_id = ctx_id
        self.nms_threshold = nms_threshold

        self._feat_stride_fpn = [32, 16, 8]
        self.anchor_cfg = {
            "8": {
                "SCALES": (2, 1),
                "BASE_SIZE": 16,
                "RATIOS": (1,),
            },
            "16": {
                "SCALES": (8, 4),
                "BASE_SIZE": 16,
                "RATIOS": (1,),
            },
            "32": {
                "SCALES": (32, 16),
                "BASE_SIZE": 16,
                "RATIOS": (1,),
            },
        }

        self.fpn_keys = []
        for s in self._feat_stride_fpn:
            self.fpn_keys.append("stride%s" % s)

        self._anchors_fpn = dict(
            zip(
                self.fpn_keys,
                generate_anchors_fpn(
                    dense_anchor=False, cfg=self.anchor_cfg
                ),
            )
        )

        for k in self._anchors_fpn:
            v = self._anchors_fpn[k].astype(np.float32)
            self._anchors_fpn[k] = v

        self._num_anchors = dict(
            zip(
                self.fpn_keys,
                [anchors.shape[0] for anchors in self._anchors_fpn.values()],
            )
        )

        self.model = load_model().to(self.device)
        self.nms = gpu_nms_wrapper(self.nms_threshold, self.ctx_id)

    def call(self, images, threshold=0.5):
        """Run the detection.

        `images` is a (N, H, W, C)-shaped array (np.float32).

        (Padding must be performed outside.)
        """
        H, W = images.shape[1:3]

        # Load the batch in to a `torch.Tensor` and pre-process by turning it
        # into a BGR format for the channels.
        data = torch.tensor(
            images.transpose([0, 3, 1, 2]),
            device=self.device, dtype=torch.float32
        ).flip(1)

        # Run the images through the network.
        net_out = [
            output.detach().to('cpu').numpy()
            for output in self.model(data)
        ]

        batch_objects = []
        for batch_idx in range(images.shape[0]):

            proposals_list = []
            scores_list = []
            landmarks_list = []
            for _idx, s in enumerate(self._feat_stride_fpn):
                stride = int(s)

                # Three per stride: class, bbox, landmark.
                idx = _idx * 3

                scores = net_out[idx]
                scores = scores[
                    [batch_idx], self._num_anchors[f'stride{s}']:, :, :
                ]

                idx += 1
                bbox_deltas = net_out[idx]
                bbox_deltas = bbox_deltas[[batch_idx], ...]

                height, width = bbox_deltas.shape[2], bbox_deltas.shape[3]

                A = self._num_anchors[f'stride{s}']
                K = height * width

                anchors_fpn = self._anchors_fpn[f'stride{s}']
                anchors = anchors_plane(height, width, stride, anchors_fpn)
                anchors = anchors.reshape((K * A, 4))

                scores = self._clip_pad(scores, (height, width))
                scores = scores.transpose((0, 2, 3, 1)).reshape((-1, 1))

                bbox_deltas = self._clip_pad(bbox_deltas, (height, width))
                bbox_deltas = bbox_deltas.transpose((0, 2, 3, 1))
                bbox_pred_len = bbox_deltas.shape[3] // A
                bbox_deltas = bbox_deltas.reshape((-1, bbox_pred_len))

                proposals = self.bbox_pred(anchors, bbox_deltas)
                proposals = clip_boxes(proposals, [H, W])

                scores_ravel = scores.ravel()
                order = np.where(scores_ravel >= threshold)[0]
                proposals = proposals[order, :]
                scores = scores[order]

                proposals_list.append(proposals)
                scores_list.append(scores)

                idx += 1
                landmark_deltas = net_out[idx]
                landmark_deltas = landmark_deltas[[batch_idx], ...]

                landmark_deltas = self._clip_pad(
                    landmark_deltas, (height, width)
                )
                landmark_pred_len = landmark_deltas.shape[1] // A
                landmark_deltas = landmark_deltas.transpose(
                    (0, 2, 3, 1)
                ).reshape((-1, 5, landmark_pred_len // 5))
                landmarks = self.landmark_pred(
                    anchors, landmark_deltas
                )
                landmarks = landmarks[order, :]
                landmarks_list.append(landmarks)

            proposals = np.vstack(proposals_list)
            landmarks = None
            if proposals.shape[0] == 0:
                batch_objects.append([])
                continue

            scores = np.vstack(scores_list)
            scores_ravel = scores.ravel()
            order = scores_ravel.argsort()[::-1]

            proposals = proposals[order, :]
            scores = scores[order]

            landmarks = np.vstack(landmarks_list)
            landmarks = landmarks[order].astype(np.float32, copy=False)

            pre_det = np.hstack((proposals[:, 0:4], scores)).astype(
                np.float32, copy=False
            )

            keep = self.nms(pre_det)
            det = np.hstack((pre_det, proposals[:, 4:]))
            det = det[keep, :]
            landmarks = landmarks[keep]

            batch_objects.append([
                {'bbox': d[:4],  'landmarks': l, 'score': d[4]}
                for d, l in zip(det, landmarks)
            ])

        return batch_objects

    @staticmethod
    def _clip_pad(tensor, pad_shape):
        """Clip boxes of the pad area.

        :param tensor: [n, c, H, W]
        :param pad_shape: [h, w]
        :return: [n, c, h, w]
        """
        H, W = tensor.shape[2:]
        h, w = pad_shape

        if h < H or w < W:
            tensor = tensor[:, :, :h, :w].copy()

        return tensor

    @staticmethod
    def bbox_pred(boxes, box_deltas):
        """Transform the set of class-agnostic boxes into class-specific boxes
        by applying the predicted offsets.

        :param boxes: !important [N 4]
        :param box_deltas: [N, 4 * num_classes]
        :return: [N 4 * num_classes]
        """
        if boxes.shape[0] == 0:
            return np.zeros((0, box_deltas.shape[1]))

        boxes = boxes.astype(np.float, copy=False)
        widths = boxes[:, 2] - boxes[:, 0] + 1.0
        heights = boxes[:, 3] - boxes[:, 1] + 1.0
        ctr_x = boxes[:, 0] + 0.5 * (widths - 1.0)
        ctr_y = boxes[:, 1] + 0.5 * (heights - 1.0)

        dx = box_deltas[:, 0:1]
        dy = box_deltas[:, 1:2]
        dw = box_deltas[:, 2:3]
        dh = box_deltas[:, 3:4]

        pred_ctr_x = dx * widths[:, np.newaxis] + ctr_x[:, np.newaxis]
        pred_ctr_y = dy * heights[:, np.newaxis] + ctr_y[:, np.newaxis]
        pred_w = np.exp(dw) * widths[:, np.newaxis]
        pred_h = np.exp(dh) * heights[:, np.newaxis]

        pred_boxes = np.zeros(box_deltas.shape)
        pred_boxes[:, 0:1] = pred_ctr_x - 0.5 * (pred_w - 1.0)
        pred_boxes[:, 1:2] = pred_ctr_y - 0.5 * (pred_h - 1.0)
        pred_boxes[:, 2:3] = pred_ctr_x + 0.5 * (pred_w - 1.0)
        pred_boxes[:, 3:4] = pred_ctr_y + 0.5 * (pred_h - 1.0)

        if box_deltas.shape[1] > 4:
            pred_boxes[:, 4:] = box_deltas[:, 4:]

        return pred_boxes

    @staticmethod
    def landmark_pred(boxes, landmark_deltas):
        if boxes.shape[0] == 0:
            return np.zeros((0, landmark_deltas.shape[1]))

        boxes = boxes.astype(np.float, copy=False)
        widths = boxes[:, 2] - boxes[:, 0] + 1.0
        heights = boxes[:, 3] - boxes[:, 1] + 1.0
        ctr_x = boxes[:, 0] + 0.5 * (widths - 1.0)
        ctr_y = boxes[:, 1] + 0.5 * (heights - 1.0)

        pred = landmark_deltas.copy()
        for i in range(5):
            pred[:, i, 0] = landmark_deltas[:, i, 0] * widths + ctr_x
            pred[:, i, 1] = landmark_deltas[:, i, 1] * heights + ctr_y

        return pred
