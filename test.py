import cv2
import torch
import os
import argparse
import random
import shutil

from utils.post_process import reorginalize_target
from models.yolov3 import yolov3
from config.yolov3 import cfg
from utils.nms import non_max_suppression
from load_data import NewDataset
from torch.utils.data import DataLoader
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
from pycocotools import mask as maskUtils
from plot_curve import ap_per_category
from plot_curve import draw_pr
from utils.build_model import build_model


class Test(object):
    def __init__(self, opt=None):
        assert opt is not None
        self.opt = opt
        self.device = torch.device(cfg.device)

        self.val_dataset = NewDataset(train_set=False)
        self.val_dataloader = DataLoader(self.val_dataset, batch_size=1, shuffle=True,
                                         num_workers=cfg.num_worker,
                                         collate_fn=self.val_dataset.collate_fn)

        self.len_train_dataset = len(self.val_dataset)

        # self.model = yolov3().to(self.device)
        self.model = build_model(opt.model)
        weights_path = self.opt.weights_path
        checkpoint = torch.load(weights_path)
        self.model.load_state_dict(checkpoint)

        self.cocoGt = COCO(cfg.test_json)

    def plot_one_box(self, x, img, color=None, label=None, line_thickness=None):  # Plots one bounding box on image img
        tl = line_thickness or round(0.001 * max(img.shape[0:2])) + 1  # line thickness
        color = color or [random.randint(0, 255) for _ in range(3)]

        cv2.line(img, (int(x[0]), int(x[1])), (int(x[2]), int(x[3])), color, tl)
        cv2.line(img, (int(x[2]), int(x[3])), (int(x[4]), int(x[5])), color, tl)
        cv2.line(img, (int(x[4]), int(x[5])), (int(x[6]), int(x[7])), color, tl)
        cv2.line(img, (int(x[6]), int(x[7])), (int(x[0]), int(x[1])), color, tl)
        cv2.putText(img, label, (int(x[0]), int(x[1])), cv2.FONT_HERSHEY_PLAIN, 1, (0, 0, 255), 1)

    def drow_box(self, anns):
        image_id = [i['image_id'] for i in anns]
        assert all(x == image_id[0] for x in image_id)
        img_ann = self.cocoGt.loadImgs(ids=image_id[0])[0]
        img_name = img_ann['file_name']
        print('images:{}'.format(img_name))
        img_path = os.path.join(opt.image_folder, img_name)
        txt_path = os.path.join(opt.output_folder, img_name.replace('png', 'txt'))
        img = cv2.imread(img_path)
        for ann in anns:
            cat = self.cocoGt.loadCats(ids=ann['category_id'])[0]
            score = ann['score']
            label = '%s %.2f' % (cat['name'], score)
            color = (0, 0, 255)
            coord = ann['segmentation'][0]
            with open(txt_path, 'a') as f:
                f.write('%s %.2f %g %g %g %g %g %g %g %g  \n' %
                        (cat['name'], score,
                         coord[0], coord[1], coord[2], coord[3], coord[4], coord[5], coord[6], coord[7]))
            self.plot_one_box(coord, img, color, label)
        cv2.imwrite(os.path.join(opt.output_folder, img_name), img)

    @torch.no_grad()
    def eval(self):
        n_threads = torch.get_num_threads()
        # FIXME remove this and make paste_masks_in_image run on the GPU
        torch.set_num_threads(n_threads)
        cpu_device = torch.device("cpu")
        self.model.eval()

        for ann_idx in self.cocoGt.anns:
            ann = self.cocoGt.anns[ann_idx]
            ann['area'] = maskUtils.area(self.cocoGt.annToRLE(ann))

        iou_types = 'segm'
        anns = []
        mAP_list = []

        for val_data in self.val_dataloader:
            image, target, logit = val_data

            image = image.to(self.device)
            image_size = image.shape[3]  # image.shape[2]==image.shape[3]
            # resize之后图像的大小

            _, pred = self.model(image)
            # TODO:当前只支持batch_size=1
            pred = pred.unsqueeze(0)
            pred = pred[pred[:, :, 8] > cfg.conf_thresh]
            detections = non_max_suppression(pred.unsqueeze(0), cls_thres=cfg.cls_thresh, nms_thres=cfg.conf_thresh)

            new_ann = reorginalize_target(detections, logit, image_size, self.cocoGt)
            self.drow_box(new_ann)
            anns.extend(new_ann)

        for ann in anns:
            ann['segmentation'] = self.cocoGt.annToRLE(ann)  # 将polygon形式的segmentation转换RLE形式

        cocoDt = self.cocoGt.loadRes(anns)

        cocoEval = COCOeval(self.cocoGt, cocoDt, iou_types)
        cocoEval.evaluate()
        cocoEval.accumulate()
        cocoEval.summarize()

        ap_per_category(self.cocoGt, cocoEval, cfg.max_epoch)
        draw_pr(self.cocoGt, cocoEval)
        print_txt = cocoEval.stats
        coco_mAP = print_txt[0]
        voc_mAP = print_txt[1]
        if isinstance(mAP_list, list):
            mAP_list.append(voc_mAP)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-image_folder', type=str, default='./data/images', help='path to images')
    parser.add_argument('-output_folder', type=str, default='result', help='path to outputs')
    parser.add_argument('-plot_flag', type=bool, default=True)
    parser.add_argument('-txt_out', type=bool, default=True)
    parser.add_argument('-cfg', type=str, default='cfg/yolov3.cfg', help='cfg file path')
    parser.add_argument('-weights_path', type=str, default='checkpoint/yolo_v5_100.pth', help='weight file path')
    parser.add_argument('-model', type=str, default='yolo_v5')
    parser.add_argument('-conf_thres', type=float, default=0.5, help='object confidence threshold')
    parser.add_argument('-nms_thres', type=float, default=0.2, help='iou threshold for non-maximum suppression')
    parser.add_argument('-batch_size', type=int, default=1, help='size of the batches')
    parser.add_argument('-img_size', type=int, default=608, help='size of each image dimension')
    opt = parser.parse_args()
    if os.path.isdir(opt.output_folder):
        shutil.rmtree(opt.output_folder)
    os.makedirs(opt.output_folder)
    test = Test(opt)
    test.eval()
