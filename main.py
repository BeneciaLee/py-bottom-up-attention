import json
import io
import os
import gc

import numpy as np
import torch

import detectron2

from tqdm import tqdm

# import some common detectron2 utilities
from detectron2.engine import DefaultPredictor
from detectron2.config import get_cfg
from detectron2.utils.visualizer import Visualizer
from detectron2.data import MetadataCatalog

from detectron2.modeling.postprocessing import detector_postprocess
from detectron2.modeling.roi_heads.fast_rcnn import FastRCNNOutputLayers, FastRCNNOutputs, fast_rcnn_inference_single_image

from torch.utils.data import Dataset, DataLoader

# import some common libraries
import numpy as np
import cv2
import torch

import pickle

# Show the image in ipynb
from IPython.display import clear_output, Image, display
import PIL.Image

def faster_rcnn(image_hashed):
    data_path = 'data/genome/1600-400-20'

    vg_classes = []
    with open(os.path.join(data_path, 'objects_vocab.txt')) as f:
        for object in f.readlines():
            vg_classes.append(object.split(',')[0].lower().strip())

    vg_attrs = []
    with open(os.path.join(data_path, 'attributes_vocab.txt')) as f:
        for object in f.readlines():
            vg_attrs.append(object.split(',')[0].lower().strip())

    MetadataCatalog.get("vg").thing_classes = vg_classes
    MetadataCatalog.get("vg").attr_classes = vg_attrs

    cfg = get_cfg()
    cfg.merge_from_file("configs/VG-Detection/faster_rcnn_R_101_C4_attr_caffemaxpool.yaml")
    cfg.MODEL.RPN.POST_NMS_TOPK_TEST = 300
    cfg.MODEL.ROI_HEADS.NMS_THRESH_TEST = 0.6
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.2
    # VG Weight

    cfg.MODEL.WEIGHTS = "faster_rcnn_from_caffe_attr.pkl"
    # cfg.MODEL.WEIGHTS = weigth
    predictor = DefaultPredictor(cfg)

    NUM_OBJECTS = 32

    def doit(raw_image):
        with torch.no_grad():
            raw_height, raw_width = raw_image.shape[:2]
            print("Original image size: ", (raw_height, raw_width))

            # Preprocessing
            image = predictor.transform_gen.get_transform(raw_image).apply_image(raw_image)
            print("Transformed image size: ", image.shape[:2])
            image = torch.as_tensor(image.astype("float32").transpose(2, 0, 1))
            inputs = [{"image": image, "height": raw_height, "width": raw_width}]
            images = predictor.model.preprocess_image(inputs)

            # Run Backbone Res1-Res4
            features = predictor.model.backbone(images.tensor)

            # Generate proposals with RPN
            proposals, _ = predictor.model.proposal_generator(images, features, None)
            proposal = proposals[0]
            print('Proposal Boxes size:', proposal.proposal_boxes.tensor.shape)

            # Run RoI head for each proposal (RoI Pooling + Res5)
            proposal_boxes = [x.proposal_boxes for x in proposals]
            features = [features[f] for f in predictor.model.roi_heads.in_features]
            box_features = predictor.model.roi_heads._shared_roi_transform(
                features, proposal_boxes
            )
            feature_pooled = box_features.mean(dim=[2, 3])  # pooled to 1x1
            print('Pooled features size:', feature_pooled.shape)

            # Predict classes and boxes for each proposal.
            pred_class_logits, pred_attr_logits, pred_proposal_deltas = predictor.model.roi_heads.box_predictor(
                feature_pooled)
            outputs = FastRCNNOutputs(
                predictor.model.roi_heads.box2box_transform,
                pred_class_logits,
                pred_proposal_deltas,
                proposals,
                predictor.model.roi_heads.smooth_l1_beta,
            )
            probs = outputs.predict_probs()[0]
            boxes = outputs.predict_boxes()[0]

            attr_prob = pred_attr_logits[..., :-1].softmax(-1)
            max_attr_prob, max_attr_label = attr_prob.max(-1)

            # Note: BUTD uses raw RoI predictions,
            #       we use the predicted boxes instead.
            # boxes = proposal_boxes[0].tensor

            # NMS
            for nms_thresh in np.arange(0.5, 1.0, 0.1):
                instances, ids = fast_rcnn_inference_single_image(
                    boxes, probs, image.shape[1:],
                    score_thresh=0.2, nms_thresh=nms_thresh, topk_per_image=NUM_OBJECTS
                )
                if len(ids) == NUM_OBJECTS:
                    break

            instances = detector_postprocess(instances, raw_height, raw_width)
            roi_features = feature_pooled[ids].detach()
            max_attr_prob = max_attr_prob[ids].detach()
            max_attr_label = max_attr_label[ids].detach()
            instances.attr_scores = max_attr_prob
            instances.attr_classes = max_attr_label

            print(instances)
            roi_features_batch = roi_features.unsqueeze(0)
            return instances, roi_features_batch

    for image_hash in image_hashed:
        im = cv2.imread(os.path.join("../datasets/images/yfcc_images", image_hash))


    print("Im : ", im.shape)

    instances, features = doit(im)
def make_turn_one_data():
    train_path = '../datasets/train_valid_deleted_candidates.json'
    caption_json_file = '../datasets/yfcc_images_added_captions.json'
    with open(train_path) as file:
        datas = json.load(file)

    with open(caption_json_file) as file:
        caption_datas = json.load(file)

    captions = []
    image_hashes = []
    dialogues = []

    """
        전체 데이터가 모두 3개의 발화를 갖고 있는 것은 아니다.
        즉, 하나의 발화만을 갖고 있는 데이터들이 존재한다.

        따라서, 3개의 발화 턴을 갖는 데이터만을 학습에 사용한다.
        밑은 이러한 발화에서 3개의 턴을 갖는 데이터만을 수집하는 과정이다.
    """
    for data in tqdm(datas):
        dialog = data['dialog']
        image_hash = data['image_hash']
        tmp = []
        """
            하나의 데이터에는 3개의 턴이 존재한다. Turn1, Turn2, Turn3가 존재한다.
            여기에서 Turn1당 스타일 정보와 발화 정보가 리스트 안에 담겨져 있다.
            이러한 스타일 정보와 발화 정보를 모두 사용하기 위해서 스타일 정보와 발화 정보를 합친다.
            -> Appreciative (Grateful)<sty>home sweet home
            이때 스타일 정보와 발화 정보 사이에 <sty>이라는 특수 토큰을 사용한다.
        """
        for d in dialog:
            tmp.append(" ".join(d))

        # 그리고 실제 데이터에서 존재하지 않는 해시명이 존재한다.
        # 이러한 해시명 같은 경우는 필요가 없으므로 제거한다.
        if (image_hash + ".jpg") not in caption_datas or len(dialog) < 1:
            continue

        dialogues.append(tmp)
        captions.append(caption_datas[image_hash + ".jpg"][0])
        image_hashes.append(image_hash + ".jpg")
    print("###############################")
    # Captions: ['a view of a road with a stop sign and a building in the background']
    # Image_hashes: ['5eaa7034d31688ef1f9bed67f1f04f49.jpg']
    # Dialogues: [['Appreciative (Grateful)<sty>home sweet home', 'Glamorous<sty>in my big house', 'Appreciative (Grateful)<sty>Its a house, so like it']]
    print("Captions: ", captions[:1])
    print("Image_hashes : ", image_hashes[:1])
    print("Dialogues : ", dialogues[:1])

    del datas, caption_datas
    gc.collect()

    features = faster_rcnn(image_hashes)
    # image_dataset_for_upscaling = ImageDatasetForUpscaling(image_hashes)
    # patches = image_resize_and_patch(image_dataset_for_upscaling, batch_size=64)
    # print("Patches : ", patches.shape)
    # torch.save(patches, "../datasets/tensor_pactched_with_turn_one.pt")
    print("Success")

def showarray(a, fmt='jpeg'):
    a = np.uint8(np.clip(a, 0, 255))
    f = io.BytesIO()
    PIL.Image.fromarray(a).save(f, fmt)
    display(Image(data=f.getvalue()))

if __name__ == "__main__":
    # Load VG Classes

    print('Shape of features:\n', features.shape)

    make_turn_one_data()