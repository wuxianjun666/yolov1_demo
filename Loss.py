import sys
import torch.nn as nn
import math
import torch

# YOLO v1算法的损失函数就包含分别关于正样本(负责预测物体的Bounding Box)和负样本(负责预测物体的Bounding Box)两部分，
# 正样本置信度为1，负样本置信度为0，正样本的损失包含置信度损失、边框回归损失和类别损失，而负样本损失只有置信度损失。
#YOLO v1的损失由5个部分组成，均使用均方差损失：
#(1)第一部分为正样本中心点坐标的损失，引入λcoord参数调节定位损失的权重。 λcoord：超参数，用于调节定位损失在整体损失中的权重
# 默认设置为5，提高了定位损失的权重，避免在训练初期，由于负样本过多导致正样本的损失在反向传播时的作用微弱进而导致模型不稳定、网络训练发散的问题。
#(2) 第二部分为正样本的宽高损失，
# YOLO v1通过对宽高进行根号处理，在一定程度上降低了网络对尺度变化的敏感程度，同时也能提高小物体宽高损失在整体目标宽高差距损失上的权重。
# 毕竟，对于大型的Bounding Box来说，小的偏差影响并不大，而对于小型的Bounding Box来说，小型的偏差就显得尤为重要。
#(3) 第三部分分别为正样本的置信度损失。
#(4)第四部分为负样本的置信度损失，引入 λnoobj 调节负样本置信度损失的权重，默认值为0.5。
# 由于负样本常常比较多，为了保证网络更多的还是学习如果正确定位正样本，因此需要将负样本的损失权重降低
# (5)第五部分是正样本的类别损失。

class Loss(nn.Module):

    def __init__(self, S=7, B=2, Classes=20, l_coord=5, l_noobj=0.5, epcoh_threshold = 400):
        super(Loss, self).__init__()
        self.S = S
        self.B = B
        self.Classes = Classes
        self.l_coord = l_coord
        self.l_noobj = l_noobj
        self.epcoh_threshold = epcoh_threshold

    def iou(self, bounding_box, ground_box, gridX, gridY, img_size=448, grid_size=64):

        # predict_box:    [centerX, centerY, width, height]
        # ground_box :    [
                            # centerX / self.grid_cell_size - indexJ,   #相对于网格的位置
                            # centerY / self.grid_cell_size - indexI,
                            # (xmax-xmin)/self.img_size,
                            # (ymax-ymin)/self.img_size,
                            # 1,xmin,ymin,xmax,ymax,(xmax-xmin)*(ymax-ymin)
        #                 ]
        # 预处理 predict_box  变为  左上X,Y  右下X,Y  两个边界点的坐标 避免浮点误差 先还原成整数
        # 不要共用引用
        # [xmin,ymin,xmax,ymax]

        predict_box = list([0, 0, 0, 0])
        predict_box[0] = (int)(gridX + bounding_box[0] * grid_size)
        predict_box[1] = (int)(gridY + bounding_box[1] * grid_size)
        predict_box[2] = (int)(bounding_box[2] * img_size)
        predict_box[3] = (int)(bounding_box[3] * img_size)

        predict_coord = list([max(0, predict_box[0] - predict_box[2] / 2),
                              max(0, predict_box[1] - predict_box[3] / 2),
                              min(img_size - 1, predict_box[0] + predict_box[2] / 2),
                              min(img_size - 1, predict_box[1] + predict_box[3] / 2)])

        predict_Area = (predict_coord[2] - predict_coord[0]) * (predict_coord[3] - predict_coord[1])
        ground_coord = list([ground_box[5].item(), ground_box[6].item(), ground_box[7].item(), ground_box[8].item()])
        ground_Area = (ground_coord[2] - ground_coord[0]) * (ground_coord[3] - ground_coord[1])

        # 2.计算交集的面积 左边的大者 右边的小者 上边的大者 下边的小者
        CrossLX = max(predict_coord[0], ground_coord[0])
        CrossRX = min(predict_coord[2], ground_coord[2])
        CrossUY = max(predict_coord[1], ground_coord[1])
        CrossDY = min(predict_coord[3], ground_coord[3])

        if CrossRX < CrossLX or CrossDY < CrossUY:  # 没有交集
            return 0

        interSection = (CrossRX - CrossLX) * (CrossDY - CrossUY)

        return interSection / (predict_Area + ground_Area - interSection)

    def forward(self,bounding_boxes, ground_truth, batch_size=32,grid_size=64, img_size=448):
        # 定义三个计算损失的变量 正样本定位损失 样本置信度损失 样本类别损失
        loss = 0
        loss_coord = 0
        loss_confidence = 0
        loss_classes = 0
        iou_sum = 0
        object_num = 0

        mseLoss = nn.MSELoss()
        for batch in range(len(bounding_boxes)):
            for indexRow in range(self.S):  # 先行 - Y
                for indexCol in range(self.S):  # 后列 - X
                    bounding_box = bounding_boxes[batch][indexRow][indexCol]
                    predict_box_one = bounding_box [0:5]
                    predict_box_two = bounding_box [5:10]
                    ground_box = ground_truth[batch][indexRow][indexCol]
                    # 1.如果此处ground_truth不存在 即只有背景 那么两个框均为负样本
                    if round(ground_box[9].item()) == 0:  # 面积为0的grount_truth 表明此处只有背景
                        loss += self.l_noobj * torch.pow(predict_box_one[4], 2) + torch.pow(predict_box_two[4], 2)
                        loss_confidence += self.l_noobj * math.pow(predict_box_one[4].item(), 2) + math.pow(predict_box_two[4].item(), 2)
                    else:
                        object_num = object_num + 1
                        predict_iou_one =  self.iou(predict_box_one, ground_box, indexCol * 64, indexRow * 64)
                        predict_iou_two = self.iou(predict_box_two, ground_box, indexCol * 64, indexRow * 64)
                        # 改进：让两个预测的box与ground box拥有更大iou的框进行拟合 让iou低的作为负样本
                        if predict_iou_one >  predict_iou_two: # 框1为正样本  框2为负样本
                            predict_box = predict_box_one
                            iou = predict_iou_one
                            no_predict_box = predict_box_two
                        else:
                            predict_box = predict_box_two
                            iou = predict_iou_two
                            no_predict_box = predict_box_one

                        # 正样本：
                        # 定位
                        loss += self.l_coord * (
                                                  torch.pow((ground_box[0] - predict_box[0]), 2)
                                                + torch.pow((ground_box[1] - predict_box[1]), 2)
                                                + torch.pow(torch.sqrt(ground_box[2] + 1e-8) - torch.sqrt(predict_box[2] + 1e-8), 2)
                                                + torch.pow(torch.sqrt(ground_box[3] + 1e-8) - torch.sqrt(predict_box[3] + 1e-8), 2)
                                                )
                        loss_coord += self.l_coord * (
                                                        math.pow((ground_box[0] - predict_box[0].item()), 2)
                                                      + math.pow((ground_box[1] - predict_box[1].item()), 2)
                                                      + math.pow(math.sqrt(ground_box[2] + 1e-8) - math.sqrt(predict_box[2].item() + 1e-8),2)
                                                      + math.pow(math.sqrt(ground_box[3] + 1e-8) - math.sqrt(predict_box[3].item() + 1e-8), 2)
                        )

                        # 置信度
                        loss += torch.pow(predict_box[4] - iou, 2)
                        loss_confidence += math.pow(predict_box[4].item() - iou, 2)
                        iou_sum = iou_sum + iou

                        # 分类
                        ground_class = ground_box[10:]
                        predict_class = bounding_box [self.B * 5:]
                        loss += mseLoss(ground_class, predict_class)
                        loss_classes += mseLoss(ground_class, predict_class).item()

                        # 负样本 置信度：
                        loss += self.l_noobj * torch.pow(no_predict_box[4] - 0, 2)
                        loss_confidence += math.pow(no_predict_box[4].item() - 0, 2)
        return loss, loss_coord, loss_confidence, loss_classes, iou_sum, object_num

    def setWeight(self, epoch):
        if epoch > self.epcoh_threshold:
            self.l_coord = 1
            self.l_noobj = 1

        '''
        for batch in range(len(bounding_boxes)):
            for indexRow in range(self.S):  # 先行 - Y
                for indexCol in range(self.S):  # 后列 - X
                    # 取bounding box中置信度更大的框 另一个框认为是无效框 不参与loss计算
                    if bounding_boxes[batch][indexRow][indexCol][4] < bounding_boxes[batch][indexRow][indexCol][9]:
                        predict_box = bounding_boxes[batch][indexRow][indexCol][5:]
                    else:
                        predict_box = bounding_boxes[batch][indexRow][indexCol][0:5]
                        predict_box = torch.cat((predict_box, bounding_boxes[batch][indexRow][indexCol][10:]), dim=0)
                    # 为拥有最大置信度的bounding_box找到最大iou的groundtruth_box
                    if (int)(ground_truth[batch][indexRow][indexCol][9].item() + 0.1) == 0:  # 面积为0的grount_truth 是为了保持ndarray每个维度上的形状相同强行拼接的无用的0-box
                        loss = loss + self.l_noobj * torch.pow(predict_box[4], 2)
                        loss_confidence += self.l_noobj * math.pow(predict_box[4].item(), 2)
                    else:
                        object_num = object_num + 1
                        iou = self.iou(predict_box, ground_truth[batch][indexRow][indexCol], indexCol * 64, indexRow * 64)
                        iou_sum = iou_sum + iou
                        ground_box = ground_truth[batch][indexRow][indexCol]
                        loss = loss + self.l_coord * (torch.pow((ground_box[0] - predict_box[0]), 2) + torch.pow((ground_box[1] - predict_box[1]), 2) + torch.pow(torch.sqrt(ground_box[2] + 1e-8) - torch.sqrt(predict_box[2] + 1e-8), 2) + torch.pow(torch.sqrt(ground_box[3] + 1e-8) - torch.sqrt(predict_box[3] + 1e-8), 2))
                        loss_coord += self.l_coord * (math.pow((ground_box[0] - predict_box[0].item()), 2) + math.pow((ground_box[1] - predict_box[1].item()), 2) + math.pow(math.sqrt(ground_box[2] + 1e-8) - math.sqrt(predict_box[2].item() + 1e-8), 2) + math.pow(math.sqrt(ground_box[3] + 1e-8) - math.sqrt(predict_box[3].item() + 1e-8), 2))
                        #confidence向着iou回归
                        loss = loss + torch.pow(predict_box[4] - iou, 2)
                        loss_confidence += math.pow(predict_box[4].item() - iou, 2)
                        ground_class = ground_box[10:]
                        predict_class = predict_box[5:]
                        loss = loss + mseLoss(ground_class,predict_class)
                        loss_classes += mseLoss(ground_class,predict_class).item()
        #print("坐标误差:{} 置信度误差:{} 类别损失:{} iou_sum:{} object_num:{} iou:{}".format(loss_coord, loss_confidence, loss_classes, iou_sum, object_num, "nan" if object_num == 0 else (iou_sum / object_num)))
'''










