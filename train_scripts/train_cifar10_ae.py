'''Train Lipschitz Autoencoder CIFAR-10 model'''
from __future__ import print_function

import logging
import os

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.optim as optim

from lib.cifar10_model import *
from lib.dataset_utils import *
from lib.lip_model import *

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"


def evaluate(net, dataloader, device):

    net.eval()
    val_loss = 0
    val_total = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(dataloader):
            inputs, targets = inputs.to(device), targets.to(device)
            latent, x_recon = net(inputs)
            loss = net.loss_function(latent, x_recon, inputs, targets)
            val_loss += loss.item()
            val_total += targets.size(0)

    return val_loss / val_total


def train(net, trainloader, validloader, optimizer, epoch, device,
          log, save_best_only=True, best_loss=0, model_path='./model.pt'):

    net.train()
    train_loss = 0
    train_total = 0
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        latent, x_recon = net(inputs)
        loss = net.loss_function(latent, x_recon, inputs, targets)
        loss.backward()
        optimizer.step()

        train_loss += loss.item()
        train_total += targets.size(0)

    val_loss = evaluate(net, validloader, device)

    log.info(' %5d | %.4f | %.4f', epoch, train_loss / train_total, val_loss)

    # Save model weights
    if not save_best_only or (save_best_only and val_loss < best_loss):
        log.info('Saving model...')
        torch.save(net.state_dict(), model_path)
        best_loss = val_loss
    return best_loss


def main():

    # Set experiment id
    exp_id = 1
    # model_name = 'lipae_mnist_exp%d' % exp_id
    model_name = 'ae_cifar10_exp%d' % exp_id
    init_it = 1
    train_it = False
    latent_dim = 128
    alpha = 1e0

    # Training parameters
    batch_size = 128
    epochs = 150
    data_augmentation = False
    learning_rate = 1e-3
    l1_reg = 0
    l2_reg = 1e-4
    use_schedule = False

    # Subtracting pixel mean improves accuracy
    subtract_pixel_mean = False

    # Set all random seeds
    seed = 2019
    np.random.seed(seed)
    torch.manual_seed(seed)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    # Set up model directory
    save_dir = os.path.join(os.getcwd(), 'saved_models')
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir)
    model_path = os.path.join(save_dir, model_name + '.h5')

    # Get logger
    log_file = model_name + '.log'
    log = logging.getLogger('train_mnist')
    log.setLevel(logging.DEBUG)
    # Create formatter and add it to the handlers
    formatter = logging.Formatter(
        '[%(levelname)s %(asctime)s %(name)s] %(message)s')
    # Create file handler
    fh = logging.FileHandler(log_file, mode='w')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    log.addHandler(fh)

    log.info(log_file)
    log.info(('CIFAR-10 | exp_id: {}, seed: {}, init_learning_rate: {}, ' +
              'batch_size: {}, l2_reg: {}, l1_reg: {}, epochs: {}, ' +
              'data_augmentation: {}, subtract_pixel_mean: {}').format(
                  exp_id, seed, learning_rate, batch_size, l2_reg, l1_reg,
                  epochs, data_augmentation, subtract_pixel_mean))

    log.info('Preparing data...')
    trainloader, validloader, testloader = load_cifar10(
        batch_size, data_dir='/data', val_size=0.1, shuffle=True, seed=seed)

    log.info('Building model...')
    # net = NCA_AE(latent_dim=latent_dim, init_it=init_it,
    #              train_it=train_it, alpha=alpha)
    net = CIFAR10_AE((3, 32, 32), latent_dim=latent_dim)
    net = net.to(device)
    # if device == 'cuda':
    #     net = torch.nn.DataParallel(net)
    #     cudnn.benchmark = True
    optimizer = optim.Adam(net.parameters(), lr=learning_rate)
    if use_schedule:
        lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, [80, 120], gamma=0.1)

    log.info(' epoch | loss  | v_loss')
    best_loss = 1e9
    for epoch in range(epochs):
        if use_schedule:
            lr_scheduler.step()
        best_loss = train(net, trainloader, validloader, optimizer,
                          epoch, device, log, save_best_only=True,
                          best_loss=best_loss, model_path=model_path)

    test_loss = evaluate(net, testloader, device)
    log.info('Test loss: %.4f', test_loss)


if __name__ == '__main__':
    main()
