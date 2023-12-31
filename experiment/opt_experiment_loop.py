import os, utils, glob, utils.losses as losses
import sys, time
from torch.utils.data import DataLoader
from data import datasets, trans
import numpy as np
import torch
from torchvision import transforms
from torch import optim
import torch.nn as nn
from natsort import natsorted
from models.ViTVNet import CONFIGS as CONFIGS_ViT
from models.ViTVNet import ViTVNet
from models.TransMorph import CONFIGS as CONFIGS_TM
import models.TransMorph as TransMorph
from models.VoxelMorph import VoxelMorph

from models.OFG import OFG
from models.CascadeOpt import CascadeOpt_Vxm, CascadeOpt_Trans
# from csv_logger import log_csv

import argparse


# parse the commandline
parser = argparse.ArgumentParser()

parser.add_argument('--train_dir', type=str, default='../autodl-fs/IXI_data/Train/')
parser.add_argument('--val_dir', type=str, default='../autodl-fs/IXI_data/Val/')
parser.add_argument('--dataset', type=str, default='IXI')
parser.add_argument('--atlas_dir', type=str, default='../autodl-fs/IXI_data/atlas.pkl')

parser.add_argument('--model', type=str, default='TransMorph')

parser.add_argument('--training_lr', type=float, default=1e-4)
parser.add_argument('--ofg_lr', type=float, default=1e-1)
parser.add_argument('--epoch_start', type=int, default=0)
parser.add_argument('--max_epoch', type=int, default=500)

parser.add_argument('--ofg_model', type=str, default='ofg')
parser.add_argument('--ofg_epoch', type=int, default=10, help='the number of iterations in optimization, 0 represents no optimization')

args = parser.parse_args()


class Logger(object):
    def __init__(self, save_dir):
        self.terminal = sys.stdout
        self.log = open(save_dir+"logfile.log", "a")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        pass


def log_csv(save_dir, epoch, dsc, Jdet, loss, lr, time):
    # check if directory exists
    if not os.path.exists('logs/'+save_dir):
        os.makedirs('logs/'+save_dir)
    with open('logs/'+save_dir+'log.csv', 'a') as f:
        f.write('{},{},{},{},{},{}\n'.format(epoch, dsc, Jdet, loss, lr, time))


def main():
    batch_size = 1
    atlas_dir = args.atlas_dir
    train_dir = args.train_dir
    val_dir = args.val_dir
    ofg_epoch = args.ofg_epoch
    
    weights_model = [1, 0.02] # loss weights of optimizer
    weights_opt = [1, 1] # loss weighs of model loss
    save_dir = '{}_{}_{}/'.format(args.model, args.dataset, args.ofg_model if args.ofg_epoch else '')
    if not os.path.exists('experiments/'+save_dir):
        os.makedirs('experiments/'+save_dir)
    if not os.path.exists('logs/'+save_dir):
        os.makedirs('logs/'+save_dir)
    sys.stdout = Logger('logs/'+save_dir)
    lr = args.training_lr # learning rate
    epoch_start = args.epoch_start
    max_epoch = args.max_epoch #max traning epoch

    '''
    Initialize model
    '''
    img_size = (160, 192, 160) if args.dataset == "LPBA" else (160, 192, 224)
    if args.model == "TransMorph":
        config = CONFIGS_TM['TransMorph']
        if args.dataset == "LPBA":
            config.img_size = img_size
            config.window_size = (5, 6, 5, 5)
        model = TransMorph.TransMorph(config)
    elif args.model == "VoxelMorph":
        model = VoxelMorph(img_size)
    elif args.model == "ViTVNet":
        config_vit = CONFIGS_ViT['ViT-V-Net']
        model = ViTVNet(config_vit, img_size=img_size)

    model.cuda()

    '''
    Initialize spatial transformation function
    '''
    reg_model = utils.register_model(img_size, 'nearest')
    reg_model.cuda()
    reg_model_bilin = utils.register_model(img_size, 'bilinear')
    reg_model_bilin.cuda()

    '''
    If continue from previous training
    '''
    if epoch_start:
        model_dir = 'experiments/'+save_dir
        updated_lr = round(lr * np.power(1 - (epoch_start) / max_epoch,0.9),8)
        best_model = torch.load(model_dir + natsorted(os.listdir(model_dir))[-1])['state_dict']
        print('Model: {} loaded!'.format(natsorted(os.listdir(model_dir))[-1]))
        model.load_state_dict(best_model)
    else:
        updated_lr = lr

    '''
    Initialize training
    '''
    if args.dataset == "IXI":
        train_composed = transforms.Compose([trans.RandomFlip(0),
                                            trans.NumpyType((np.float32, np.float32)),
                                            ])

        val_composed = transforms.Compose([trans.Seg_norm(), #rearrange segmentation label to 1 to 46
                                        trans.NumpyType((np.float32, np.int16))])
        
        train_set = datasets.IXIBrainDataset(glob.glob(train_dir + '*.pkl'), atlas_dir, transforms=train_composed)
        val_set = datasets.IXIBrainInferDataset(glob.glob(val_dir + '*.pkl'), atlas_dir, transforms=val_composed)
        train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=0, pin_memory=True)
        val_loader = DataLoader(val_set, batch_size=1, shuffle=False, num_workers=0, pin_memory=True, drop_last=True)
    elif args.dataset == "OASIS":
        train_composed = transforms.Compose([trans.NumpyType((np.float32, np.int16))])
        val_composed = transforms.Compose([trans.NumpyType((np.float32, np.int16))])
        train_set = datasets.OASISBrainDataset(glob.glob(train_dir + '*.pkl'), transforms=train_composed)
        val_set = datasets.OASISBrainInferDataset(glob.glob(val_dir + '*.pkl'), transforms=val_composed)
        train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=0, pin_memory=True)
        val_loader = DataLoader(val_set, batch_size=1, shuffle=False, num_workers=0, pin_memory=True, drop_last=True)
    
    optimizer = optim.Adam(model.parameters(), lr=updated_lr, weight_decay=0, amsgrad=True)
    criterion = losses.NCC_vxm()
    criterions = [criterion]
    criterions += [losses.Grad3d(penalty='l2')]
    criterions += [nn.MSELoss()]
    best_dsc = 0

    if ofg_epoch:
        if args.ofg_model == "CascadeOpt_Vxm":
            ofg = CascadeOpt_Vxm(img_size, blk_num=2).cuda()
        elif args.ofg_model == "CascadeOpt_Trans":
            ofg = CascadeOpt_Trans().cuda()
        elif args.ofg_model == "CNNOpt":
            ofg = CascadeOpt_Vxm(img_size).cuda()
        ofg_optimizer = optim.Adam(ofg.parameters(), lr=args.ofg_lr, weight_decay=0, amsgrad=True)

    for epoch in range(epoch_start, max_epoch):
        print('Training Starts')
        start_time = time.time()
        '''
        Training
        '''
        loss_all = utils.AverageMeter()
        idx = 0
        for data in train_loader:
            idx += 1
            model.train()
            adjust_learning_rate(optimizer, epoch, max_epoch, lr)
            data = [t.cuda() for t in data]
            x = data[0]
            y = data[1]
            if args.dataset == "OASIS":
                x_seg = data[2]
                y_seg = data[3]
            x_in = torch.cat((x,y), dim=1)
            output = model(x_in)

            if ofg_epoch:
                if args.ofg_model == "ofg":
                    ofg = OFG(output[1].clone().detach())
                    ofg_optimizer = optim.Adam(ofg.parameters(), lr=args.ofg_lr, weight_decay=0, amsgrad=True)

                adjust_learning_rate(ofg_optimizer, epoch, max_epoch, args.ofg_lr)

                for _ in range(args.ofg_epoch):
                    x_warped, optimized_flow = ofg(torch.cat([output[0], y], dim=1).clone().detach())
                    ofg_loss_ncc = criterions[0](x_warped, y) * weights_opt[0]
                    ofg_loss_reg = criterions[1](optimized_flow, y) * weights_opt[1]
                    ofg_loss = ofg_loss_ncc + ofg_loss_reg

                    ofg_optimizer.zero_grad()
                    ofg_loss.backward()
                    ofg_optimizer.step()
                
                x_warped, optimized_flow = ofg(torch.cat([output[0], y], dim=1).clone().detach())
            
                loss_mse = criterions[2](output[1], optimized_flow) * weights_model[0]
                loss_reg = criterions[1](output[1], y) * weights_model[1]
                loss = loss_mse + loss_reg
                loss_vals = [loss_mse, loss_reg]
                loss_all.update(loss.item(), y.numel())
            else:
                loss_ncc = criterions[0](output[0], y)
                loss_reg = criterions[1](output[1], y)
                loss = loss_ncc + loss_reg
                loss_vals = [loss_ncc, loss_reg]
                loss_all.update(loss.item(), y.numel())

            # compute gradient and do SGD step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if args.dataset == "OASIS":
                y_in = torch.cat((y, x), dim=1)
                output = model(y_in)

                if ofg_epoch:
                    ofg2 = OFG(output[1].clone().detach())
                    ofg_optimizer = optim.Adam(ofg2.parameters(), lr=args.ofg_lr, weight_decay=0, amsgrad=True)
                    adjust_learning_rate(ofg_optimizer, epoch, max_epoch, args.ofg_lr)

                    for _ in range(ofg_epoch):
                        y_warped, optimized_flow = ofg2(y)
                        ofg_loss_ncc = criterions[0](y_warped, x) * weights_opt[0]
                        ofg_loss_reg = criterions[1](optimized_flow, x) * weights_opt[1]
                        ofg_loss = ofg_loss_ncc + ofg_loss_reg

                        ofg_optimizer.zero_grad()
                        ofg_loss.backward()
                        ofg_optimizer.step()

                    loss_mse = criterions[2](optimized_flow, output[1]) * weights_model[0]
                    loss_reg = criterions[1](output[1], x) * weights_model[1]
                    loss = loss_mse + loss_reg
                    loss_vals = [loss_mse, loss_reg]
                else:
                    loss_ncc = criterions[0](output[0], x)
                    loss_reg = criterions[1](output[1], x)
                    loss = loss_ncc + loss_reg
                    loss_vals = [loss_ncc, loss_reg]
                
                loss_all.update(loss.item(), x.numel())
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            current_lr = optimizer.state_dict()['param_groups'][0]['lr']
            
            print('Epoch [{}/{}] Iter [{}/{}] - loss {:.4f}, Img Sim: {:.6f}, Reg: {:.6f}, lr: {:.6f}'.format(epoch, max_epoch, idx, len(train_loader), loss.item(), loss_vals[0].item(), loss_vals[1].item(), current_lr))

        # print('Epoch {} loss {:.4f}'.format(epoch, loss_all.avg))
        '''
        Validation
        '''
        eval_dsc = utils.AverageMeter()
        eval_det = utils.AverageMeter()
        with torch.no_grad():
            for data in val_loader:
                model.eval()
                data = [t.cuda() for t in data]
                x, y, x_seg, y_seg = data
                x_in = torch.cat((x, y), dim=1)
                output = model(x_in)
                def_out = reg_model([x_seg.cuda().float(), output[1].cuda()])
                if args.dataset == "OASIS":
                    dsc = utils.dice_OASIS(def_out.long(), y_seg.long())
                elif args.dataset == "IXI":
                    dsc = utils.dice_IXI(def_out.long(), y_seg.long())
                eval_dsc.update(dsc.item(), x.size(0))
                jac_det = utils.jacobian_determinant_vxm(output[1].detach().cpu().numpy()[0, :, :, :, :])
                tar = y.detach().cpu().numpy()[0, 0, :, :, :]
                eval_det.update(np.sum(jac_det <= 0) / np.prod(tar.shape), x.size(0))
        best_dsc = max(eval_dsc.avg, best_dsc)
        
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_dsc': best_dsc,
            'optimizer': optimizer.state_dict(),
        }, save_dir='experiments/'+save_dir, filename='dsc{:.3f}_epoch{:d}.pth.tar'.format(eval_dsc.avg, epoch))
        
        end_time = time.time()
        elapsed_time = end_time - start_time
        print('\nEpoch [{}/{}] - DSC: {:.6f}, Jdet: {:.8f}, loss: {:.6f}, lr: {:.6f}, Time: {:.6f}\n'.format(epoch, max_epoch, eval_dsc.avg, eval_det.avg, loss_all.avg, current_lr, elapsed_time))
        log_csv(save_dir, epoch, eval_dsc.avg, eval_det.avg, loss_all.avg, current_lr, elapsed_time)
        loss_all.reset()
        torch.cuda.empty_cache()

def adjust_learning_rate(optimizer, epoch, MAX_EPOCHES, INIT_LR, power=0.9):
    for param_group in optimizer.param_groups:
        param_group['lr'] = round(INIT_LR * np.power( 1 - (epoch) / MAX_EPOCHES ,power),8)


def save_checkpoint(state, save_dir='models', filename='checkpoint.pth.tar', max_model_num=8):
    torch.save(state, save_dir+filename)
    model_lists = natsorted(glob.glob(save_dir + '*'))
    while len(model_lists) > max_model_num:
        os.remove(model_lists[0])
        model_lists = natsorted(glob.glob(save_dir + '*'))

if __name__ == '__main__':
    '''
    GPU configuration
    '''
    GPU_iden = 0
    GPU_num = torch.cuda.device_count()
    print('Number of GPU: ' + str(GPU_num))
    for GPU_idx in range(GPU_num):
        GPU_name = torch.cuda.get_device_name(GPU_idx)
        print('     GPU #' + str(GPU_idx) + ': ' + GPU_name)
    torch.cuda.set_device(GPU_iden)
    GPU_avai = torch.cuda.is_available()
    print('Currently using: ' + torch.cuda.get_device_name(GPU_iden))
    print('If the GPU is available? ' + str(GPU_avai))
    main()
