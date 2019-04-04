'''MNIST models'''

import copy
import random

import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions.normal import Normal

import faiss
from lib.faiss_utils import *


class BasicModel(nn.Module):

    def __init__(self, num_classes=10):
        super(BasicModel, self).__init__()
        self.conv1 = nn.Conv2d(1, 64, kernel_size=8, stride=2, padding=3)
        self.relu1 = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv2d(64, 128, kernel_size=6, stride=2, padding=3)
        self.relu2 = nn.ReLU(inplace=True)
        self.conv3 = nn.Conv2d(128, 128, kernel_size=5, stride=1, padding=0)
        self.relu3 = nn.ReLU(inplace=True)
        self.fc = nn.Linear(2048, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.conv1(x)
        x = self.relu1(x)
        x = self.conv2(x)
        x = self.relu2(x)
        x = self.conv3(x)
        x = self.relu3(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x


class DKNN(object):

    def __init__(self, model, x_train, y_train, x_cal, y_cal, layers, k=75,
                 num_classes=10, device='cuda'):
        """
        device: device that model is on
        """
        # self.model = copy.deepcopy(model)
        self.model = model
        self.x_train = x_train
        self.y_train = y_train
        self.layers = layers
        self.k = k
        self.num_classes = num_classes
        self.device = device
        self.indices = []
        self.activations = {}

        # register hook to get representations
        layer_count = 0
        for name, module in self.model.named_children():
            if name in layers:
                module.register_forward_hook(self._get_activation(name))
                layer_count += 1
        assert layer_count == len(layers)
        reps = self.get_activations(x_train)

        for layer in layers:
            rep = reps[layer].cpu().view(x_train.size(0), -1)
            # normalize activations so inner product is cosine similarity
            index = self._build_index(rep.renorm(2, 0, 1))
            self.indices.append(index)

        # set up calibration for credibility score
        y_pred = self.classify(x_cal)
        self.A = np.zeros((x_cal.size(0), )) + self.k * len(self.layers)
        for i, (y_c, y_p) in enumerate(zip(y_cal, y_pred)):
            self.A[i] -= y_p[y_c]

    def _get_activation(self, name):
        def hook(model, input, output):
            # TODO: detach() is removed to get gradients
            self.activations[name] = output
        return hook

    def _build_index(self, xb):

        d = xb.size(-1)
        # res = faiss.StandardGpuResources()
        # index = faiss.GpuIndexFlatIP(res, d)

        # brute-force
        # index = faiss.IndexFlatIP(d)

        # quantizer = faiss.IndexFlatL2(d)
        # index = faiss.IndexIVFFlat(quantizer, d, 100)
        # index.train(xb.cpu().numpy())

        # locality-sensitive hash
        index = faiss.IndexLSH(d, 256)

        index.add(xb.detach().cpu().numpy())
        return index

    def get_activations(self, x):
        _ = self.model(x.to(self.device))
        return self.activations

    def get_neighbors(self, x, k=None, layers=None):
        if k is None:
            k = self.k
        if layers is None:
            layers = self.layers
        output = []
        reps = self.get_activations(x)
        for layer, index in zip(self.layers, self.indices):
            if layer in layers:
                rep = reps[layer].renorm(2, 0, 1)
                rep = rep.detach().cpu().numpy().reshape(x.size(0), -1)
                D, I = index.search(rep, k)
                # D, I = search_index_pytorch(index, reps[layer], k)
                # uncomment when using GPU
                # res.syncDefaultStreamCurrentDevice()
                output.append((D, I))
        return output

    def classify(self, x):
        """return number of k-nearest neighbors in each class"""
        nb = self.get_neighbors(x)
        class_counts = np.zeros((x.size(0), self.num_classes))
        for (_, I) in nb:
            y_pred = self.y_train.cpu().numpy()[I]
            for i in range(x.size(0)):
                class_counts[i] += np.bincount(y_pred[i], minlength=10)
        return class_counts

    def credibility(self, class_counts):
        """compute credibility of samples given their class_counts"""
        alpha = self.k * len(self.layers) - np.max(class_counts, 1)
        cred = np.zeros_like(alpha)
        for i, a in enumerate(alpha):
            cred[i] = np.sum(self.A >= a)
        return cred / self.A.shape[0]


class ClassAuxVAE(nn.Module):

    def __init__(self, input_dim, num_classes=10, latent_dim=20):
        super(ClassAuxVAE, self).__init__()
        self.input_dim = input_dim
        self.latent_dim = latent_dim
        self.input_dim_flat = 1
        for dim in input_dim:
            self.input_dim_flat *= dim
        self.en_conv1 = nn.Conv2d(1, 64, kernel_size=8, stride=2, padding=3)
        self.relu1 = nn.ReLU(inplace=True)
        self.en_conv2 = nn.Conv2d(64, 128, kernel_size=6, stride=2, padding=3)
        self.relu2 = nn.ReLU(inplace=True)
        self.en_conv3 = nn.Conv2d(128, 128, kernel_size=5, stride=1, padding=0)
        self.relu3 = nn.ReLU(inplace=True)
        self.en_fc1 = nn.Linear(2048, 128)
        self.relu4 = nn.ReLU(inplace=True)
        self.en_mu = nn.Linear(128, latent_dim)
        self.en_logvar = nn.Linear(128, latent_dim)

        self.de_fc1 = nn.Linear(latent_dim, 128)
        self.de_fc2 = nn.Linear(128, self.input_dim_flat * 2)

        # TODO: experiment with different auxilary architecture
        self.ax_fc1 = nn.Linear(latent_dim, 128)
        self.ax_fc2 = nn.Linear(128, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def encode(self, x):
        x = self.relu1(self.en_conv1(x))
        x = self.relu2(self.en_conv2(x))
        x = self.relu3(self.en_conv3(x))
        x = x.view(x.size(0), -1)
        x = self.relu4(self.en_fc1(x))
        en_mu = self.en_mu(x)
        # TODO: use tanh activation on logvar if unstable
        # en_std = torch.exp(0.5 * x[:, self.latent_dim:])
        en_logvar = self.en_logvar(x)
        return en_mu, en_logvar

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return mu + eps * std

    def decode(self, z):
        x = F.relu(self.de_fc1(z))
        x = self.de_fc2(x)
        de_mu = x[:, :self.input_dim_flat]
        # de_std = torch.exp(0.5 * x[:, self.input_dim_flat:])
        de_logvar = x[:, self.input_dim_flat:].tanh()
        out_dim = (z.size(0), ) + self.input_dim
        return de_mu.view(out_dim).sigmoid(), de_logvar.view(out_dim)

    def auxilary(self, z):
        x = F.relu(self.ax_fc1(z))
        x = self.ax_fc2(x)
        return x

    def forward(self, x):
        en_mu, en_logvar = self.encode(x)
        z = self.reparameterize(en_mu, en_logvar)
        de_mu, de_logvar = self.decode(z)
        y = self.auxilary(z)
        return en_mu, en_logvar, de_mu, de_logvar, y


class SNNLModel(nn.Module):

    def __init__(self, num_classes=10, train_it=False):
        super(SNNLModel, self).__init__()
        self.conv1 = nn.Conv2d(1, 64, kernel_size=8, stride=2, padding=3)
        self.relu1 = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv2d(64, 128, kernel_size=6, stride=2, padding=3)
        self.relu2 = nn.ReLU(inplace=True)
        self.conv3 = nn.Conv2d(128, 128, kernel_size=5, stride=1, padding=0)
        self.relu3 = nn.ReLU(inplace=True)
        self.fc = nn.Linear(2048, num_classes)

        # initialize inverse temperature for each layer
        self.it = torch.nn.Parameter(
            data=torch.tensor([-4.6, -4.6, -4.6]), requires_grad=train_it)

        # set up hook to get representations
        self.layers = ['relu1', 'relu2', 'relu3']
        self.activations = {}
        for name, module in self.named_children():
            if name in self.layers:
                module.register_forward_hook(self._get_activation(name))

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def _get_activation(self, name):
        def hook(model, input, output):
            self.activations[name] = output
        return hook

    def forward(self, x):
        x = self.conv1(x)
        x = self.relu1(x)
        x = self.conv2(x)
        x = self.relu2(x)
        x = self.conv3(x)
        x = self.relu3(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x

    def loss_function(self, x, y_target, alpha=-1):
        """soft nearest neighbor loss"""
        snn_loss = torch.zeros(1).cuda()
        y_pred = self.forward(x)
        for l, layer in enumerate(self.layers):
            rep = self.activations[layer]
            rep = rep.view(x.size(0), -1)
            for i in range(x.size(0)):
                mask_same = (y_target[i] == y_target).type(torch.float32)
                mask_self = torch.ones(x.size(0)).cuda()
                mask_self[i] = 0
                dist = ((rep[i] - rep) ** 2).sum(1) * self.it[l].exp()
                # dist = ((rep[i] - rep) ** 2).sum(1) * 0.01
                # TODO: get nan gradients at
                # Function 'MulBackward0' returned nan values in its 1th output.
                exp = torch.exp(- torch.min(dist, torch.tensor(50.).cuda()))
                # exp = torch.exp(- dist)
                snn_loss += torch.log(torch.sum(mask_self * mask_same * exp) /
                                      torch.sum(mask_self * exp))

        ce_loss = F.cross_entropy(y_pred, y_target)
        return y_pred, ce_loss - alpha / x.size(0) * snn_loss


class HiddenMixupModel(nn.Module):

    def __init__(self, num_classes=10):
        super(HiddenMixupModel, self).__init__()
        self.conv1 = nn.Conv2d(1, 64, kernel_size=8, stride=2, padding=3)
        self.relu1 = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv2d(64, 128, kernel_size=6, stride=2, padding=3)
        self.relu2 = nn.ReLU(inplace=True)
        self.conv3 = nn.Conv2d(128, 128, kernel_size=5, stride=1, padding=0)
        self.relu3 = nn.ReLU(inplace=True)
        self.fc = nn.Linear(2048, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x, target=None, mixup_hidden=False, mixup_alpha=0.1,
                layer_mix=None):

        if mixup_hidden:
            if layer_mix is None:
                # TODO: which layers?
                layer_mix = random.randint(0, 4)

            if layer_mix == 0:
                x, y_a, y_b, lam = self.mixup_data(x, target, mixup_alpha)
            x = self.conv1(x)
            x = self.relu1(x)

            if layer_mix == 1:
                x, y_a, y_b, lam = self.mixup_data(x, target, mixup_alpha)
            x = self.conv2(x)
            x = self.relu2(x)

            if layer_mix == 2:
                x, y_a, y_b, lam = self.mixup_data(x, target, mixup_alpha)
            x = self.conv3(x)
            x = self.relu3(x)

            if layer_mix == 3:
                x, y_a, y_b, lam = self.mixup_data(x, target, mixup_alpha)
            x = x.view(x.size(0), -1)
            x = self.fc(x)

            if layer_mix == 4:
                x, y_a, y_b, lam = self.mixup_data(x, target, mixup_alpha)

            # lam = torch.tensor(lam).cuda()
            # lam = lam.repeat(y_a.size())
            return x, y_a, y_b, lam

        else:
            x = self.conv1(x)
            x = self.relu1(x)
            x = self.conv2(x)
            x = self.relu2(x)
            x = self.conv3(x)
            x = self.relu3(x)
            x = x.view(x.size(0), -1)
            x = self.fc(x)
            return x

    @staticmethod
    def loss_function(y_pred, y_a, y_b, lam):
        loss = lam * F.cross_entropy(y_pred, y_a) + \
            (1 - lam) * F.cross_entropy(y_pred, y_b)
        return loss

    @staticmethod
    def mixup_data(x, y, alpha):
        '''
        Compute the mixup data. Return mixed inputs, pairs of targets, and
        lambda. Code from
        https://github.com/vikasverma1077/manifold_mixup/blob/master/supervised/models/utils.py
        '''
        if alpha > 0.:
            lam = np.random.beta(alpha, alpha)
        else:
            lam = 1.
        index = torch.randperm(x.size(0)).cuda()
        mixed_x = lam * x + (1 - lam) * x[index, :]
        y_a, y_b = y, y[index]
        return mixed_x, y_a, y_b, lam