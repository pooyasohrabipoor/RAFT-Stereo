import torch
import torch.nn as nn
import torch.nn.functional as F
from core.update import BasicMultiUpdateBlock
from core.extractor import BasicEncoder, MultiBasicEncoder, ResidualBlock
from core.corr import CorrBlock1D, PytorchAlternateCorrBlock1D, CorrBlockFast1D, AlternateCorrBlock
from core.utils.utils import coords_grid, upflow8


try:
    autocast = torch.cuda.amp.autocast
except:
    # dummy autocast for PyTorch < 1.6
    class autocast:
        def __init__(self, enabled):
            pass
        def __enter__(self):
            pass
        def __exit__(self, *args):
            pass

class RAFTStereo(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        
        context_dims = args.hidden_dims

        self.cnet = MultiBasicEncoder(output_dim=[args.hidden_dims, context_dims], norm_fn=args.context_norm, downsample=args.n_downsample) # This one extract features and the context_dims is the channel dimmention of feature maps use fro context encoder and hidden_dims is number of channels of feature map used for feature encoder 
        self.update_block = BasicMultiUpdateBlock(self.args, hidden_dims=args.hidden_dims) ## This one is for GRU and based on your hidden_dims we have number of GRU cells per time frame. if hidden_dims=[123,120,90] then we have 3 level of feature maps then we have 3 GRU units per time frame and each unit is for another level of feautre

        self.context_zqr_convs = nn.ModuleList([nn.Conv2d(context_dims[i], args.hidden_dims[i]*3, 3, padding=3//2) for i in range(self.args.n_gru_layers)])  # This line defines input and outputs of GRU which are(z(gamau) = update gate r(gama r) = reset gate q (C(t)) = candidate hidden state input

        if args.shared_backbone:
            self.conv2 = nn.Sequential(
                ResidualBlock(128, 128, 'instance', stride=1),
                nn.Conv2d(128, 256, 3, padding=1))
        else:
            self.fnet = BasicEncoder(output_dim=256, norm_fn='instance', downsample=args.n_downsample)

    def freeze_bn(self):
        for m in self.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eval()

    def initialize_flow(self, img):
        """ Flow is represented as difference between two coordinate grids flow = coords1 - coords0"""
        N, _, H, W = img.shape

        coords0 = coords_grid(N, H, W).to(img.device)
        coords1 = coords_grid(N, H, W).to(img.device)

        return coords0, coords1

    def upsample_flow(self, flow, mask): # flow filed = disparity map and here this function tries to upsample disparity map but with 3x3 kernels with learnable weights
        """ Upsample flow field [H/8, W/8, 2] -> [H, W, 2] using convex combination """
        N, D, H, W = flow.shape
        factor = 2 ** self.args.n_downsample
        mask = mask.view(N, 1, 9, factor, factor, H, W)
        mask = torch.softmax(mask, dim=2)

        up_flow = F.unfold(factor * flow, [3,3], padding=1)
        up_flow = up_flow.view(N, D, 9, 1, 1, H, W)

        up_flow = torch.sum(mask * up_flow, dim=2)
        up_flow = up_flow.permute(0, 1, 4, 2, 5, 3)
        return up_flow.reshape(N, D, factor*H, factor*W)


    def forward(self, image1, image2, iters=12, flow_init=None, test_mode=False):
        """ Estimate optical flow between pair of frames """

        image1 = (2 * (image1 / 255.0) - 1.0).contiguous()
        image2 = (2 * (image2 / 255.0) - 1.0).contiguous()

        # run the context network
        with autocast(enabled=self.args.mixed_precision):
            if self.args.shared_backbone:## get the features of right and left image
                *cnet_list, x = self.cnet(torch.cat((image1, image2), dim=0), dual_inp=True, num_layers=self.args.n_gru_layers) ### x is final feature map of encoder but cnet_list is a tuple like this cnet_list = [(net1, inp1), (net2, inp2), (net3, inp3)]  net is (initial hidden state for the GRU) and inp is (context features that stay fixed through iterations) if we take three different GRU units per ittertaion
                #torch.cat((image1, image2), dim=0) concatinate two image along their batch so that both images will be fed through encoder at the same time to let sharing weights
                fmap1, fmap2 = self.conv2(x).split(dim=0, split_size=x.shape[0]//2)  ### get the feature maps of right and left img ( since they are stacked together we split them here)
            else:
                cnet_list = self.cnet(image1, num_layers=self.args.n_gru_layers)
                fmap1, fmap2 = self.fnet([image1, image2])
            net_list = [torch.tanh(x[0]) for x in cnet_list] # getting nets of different levels of GRU in the same itteration which is the first hidden state of GRU
            inp_list = [torch.relu(x[1]) for x in cnet_list]# getting Inps of different levels of GRU (context features that are also input of GRU)

            # Rather than running the GRU's conv layers on the context features multiple times, we do it once at the beginning 
            inp_list = [list(conv(i).split(split_size=conv.out_channels//3, dim=1)) for i,conv in zip(inp_list, self.context_zqr_convs)] # this takes the input from inp_list and use the corresponding conv from context_zqr_convs the weights of this conv is (wc,wu,wt) which is ebing multiplied to the input (X) only not to the previous hidden state but becasue input stays the same we first compute this weight and later on during updates we compute weights that multiplies to the previous hidden state
                # so the conv they use here has enough output channels that the out put includes all (wc,wu,wt) so they split it by 3 to get each individually
        if self.args.corr_implementation == "reg": # Default
            corr_block = CorrBlock1D         
            fmap1, fmap2 = fmap1.float(), fmap2.float()
        elif self.args.corr_implementation == "alt": # More memory efficient than reg
            corr_block = PytorchAlternateCorrBlock1D
            fmap1, fmap2 = fmap1.float(), fmap2.float()
        elif self.args.corr_implementation == "reg_cuda": # Faster version of reg
            corr_block = CorrBlockFast1D
        elif self.args.corr_implementation == "alt_cuda": # Faster version of alt
            corr_block = AlternateCorrBlock
        corr_fn = corr_block(fmap1, fmap2, radius=self.args.corr_radius, num_levels=self.args.corr_levels)  # all I know is takes feature maps and provide corrolation pyramids

        coords0, coords1 = self.initialize_flow(net_list[0])

        if flow_init is not None:
            coords1 = coords1 + flow_init

        flow_predictions = []
        for itr in range(iters): # do the iterations ( iters is max of time=t) and update_block takes input and previous hidden state of GRU and give you new hidden state(nnet_list) and new dis map(delta_flow) and then upsampling 
            coords1 = coords1.detach()
            corr = corr_fn(coords1) # index correlation volume
            flow = coords1 - coords0
            with autocast(enabled=self.args.mixed_precision):
                if self.args.n_gru_layers == 3 and self.args.slow_fast_gru: # Update low-res GRU
                    net_list = self.update_block(net_list, inp_list, iter32=True, iter16=False, iter08=False, update=False)
                if self.args.n_gru_layers >= 2 and self.args.slow_fast_gru:# Update low-res GRU and mid-res GRU
                    net_list = self.update_block(net_list, inp_list, iter32=self.args.n_gru_layers==3, iter16=True, iter08=False, update=False)
                net_list, up_mask, delta_flow = self.update_block(net_list, inp_list, corr, flow, iter32=self.args.n_gru_layers==3, iter16=self.args.n_gru_layers>=2)

            # in stereo mode, project flow onto epipolar
            delta_flow[:,1] = 0.0

            # F(t+1) = F(t) + \Delta(t)
            coords1 = coords1 + delta_flow

            # We do not need to upsample or output intermediate results in test_mode
            if test_mode and itr < iters-1:
                continue

            # upsample predictions
            if up_mask is None:
                flow_up = upflow8(coords1 - coords0)
            else:
                flow_up = self.upsample_flow(coords1 - coords0, up_mask)
            flow_up = flow_up[:,:1]

            flow_predictions.append(flow_up)

        if test_mode:
            return coords1 - coords0, flow_up

        return flow_predictions
