"""
Runs tests for nowcast model
"""
import sys
sys.path.append('src/')

import os
import h5py
import argparse
import pandas as pd
import numpy as np
import tensorflow as tf
os.environ["HDF5_USE_FILE_LOCKING"]='FALSE'

from tqdm import tqdm
from metrics import probability_of_detection,success_rate
from metrics.histogram import compute_histogram,score_histogram
from losses import lpips
from metrics import probability_of_detection,success_rate
from metrics.lpips_metric import get_lpips

from readers.nowcast_reader import get_data

norm = {'scale':47.54,'shift':33.44}

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test_data', type=str, default='mse',help='Test data .h5 file')
    parser.add_argument('--model', type=str, help='Path to pretrained model to test or the string "pers" for the persistence model')
    parser.add_argument('--output', type=str, help='Name of output .csv file',default='synrad_test_output.csv')  
    parser.add_argument('--batch_size', type=int, help='batch size for testing', default=32)
    parser.add_argument('--num_test', type=int, help='number of testing samples to use (None = all samples)', default=None)
    parser.add_argument('--crop', type=int, help='crops this many pixels along edge before scoring', default=0)
    args, unknown = parser.parse_known_args()

    return args


# Optical flow model using rainy motion library
norm = {'scale':47.54,'shift':33.44}
class OpticalFlow:
    def __init__(self,n_out=12):
        self.n_out=n_out
    def fit(self,X,y):
        # Doesn't need a fit
        pass
    def predict(self,X):
        """
        To make compatible with the CNNs that were trained, we assume data is input scaled.
        Data is unscaled to [0-255] before being passed to Dense(), and then rescaled back.
        """
        y_pred = np.zeros((X.shape[0],X.shape[1],X.shape[2],self.n_out),dtype=np.float32)
        to_input = np.transpose(X, [0, 3, 1, 2]) # rainy motion expects [T, L, W]
        
        # Run optical flow on each sample
        from rainymotion.models import Dense
        model = Dense()
        # keep rainymotion defaults
        model.lead_steps = self.n_out
        model.of_method = "DIS"
        model.direction = "backward"
        model.advection = "constant-vector"
        model.interpolation = "idw"
        for x in range(to_input.shape[0]):
            model.input_data = to_input[x]*norm['scale']+norm['shift']
            to_output = model.run()
            y_pred[x] = np.transpose(to_output, [1, 2, 0]) # back to [L,W,T]
        return (y_pred-norm['shift'])/norm['scale']

def ssim(y_true,y_pred,maxVal,**kwargs):
    yt=tf.convert_to_tensor(y_true.astype(np.uint8))
    yp=tf.convert_to_tensor(y_pred.astype(np.uint8))
    s=tf.image.ssim_multiscale(
              yt, yp, max_val=maxVal[0], filter_size=11, filter_sigma=1.5, k1=0.01, k2=0.03
    )
    return tf.reduce_mean(s)

def MAE(y_true,y_pred,dum):
    return tf.reduce_mean(tf.keras.losses.MAE(y_true,y_pred))

def MSE(y_true,y_pred,dum):
    return tf.reduce_mean(tf.keras.losses.MSE(y_true,y_pred))

def run_metric( metric, thres, y_true, y_pred, batch_size):
    result = 0.0
    Ltot = 0.0
    n_batches = int(np.ceil(y_true.shape[0]/batch_size))
    print('Running metric ',metric.__name__,'with thres=',thres)
    for b in range(n_batches):
        start = b*batch_size
        end   = min((b+1)*batch_size,y_true.shape[0])
        L = end-start
        yt = y_true[start:end]
        yp = y_pred[start:end]
        result += metric(yt.astype(np.float32),yp,np.array([thres],dtype=np.float32))*L
        Ltot+=L
    return (result / Ltot).numpy() 

def run_histogram(y_true, y_pred, batch_size=1000,bins=range(255)):
    L = len(bins)-1
    H = np.zeros( (L,L),dtype=np.float64) 
    n_batches = int(np.ceil(y_true.shape[0]/batch_size))
    print('Computing histogram ')
    for b in range(n_batches):
        start = b*batch_size
        end   = min((b+1)*batch_size,y_true.shape[0])
        yt = y_true[start:end]
        yp = y_pred[start:end]
        Hi,rb,cb = compute_histogram(yt,yp,bins)
        H+=Hi
    return H,rb,cb 

def main():
    args = get_args()
    model_file      = args.model
    test_data_file  = args.test_data
    output_csv_file = args.output
    crop            = args.crop

    print('get data')
    x_test, y_test, _, _ = get_data(args.test_data, end=args.num_test, pct_validation=0)
    print(f'x_test : {x_test.shape}')
    print('predict')

    if args.model=='pers':
        print('Using persistence model')
        # only keep the data for persistence
        x_test = x_test[...,11:12] 
        x_test = x_test*norm['scale']+norm['shift']
        y_pred = np.concatenate(12*[x_test], axis=-1) 
        x_test = None # just to release memory ...
        print(f'persistence data : {y_pred.shape}')
    elif args.model=='optflow':
        print('Using optical flow model')
        of=OpticalFlow()
        y_pred=of.predict(x_test)
        x_test = None # just to release memory ...
        y_pred = y_pred*norm['scale']+norm['shift']
        print(f'y_pred : {y_pred.shape}')
    else:
        print('loading model')
        model = tf.keras.models.load_model(model_file,compile=False,custom_objects={"tf": tf})
        y_pred = model.predict(x_test, batch_size=16, verbose=2)
        x_test = None # just to release memory ...
        # scale predictions back to original scale and mean
        if type(y_pred) == list:
            y_pred = y_pred[0]
        y_pred = y_pred*norm['scale']+norm['shift']
        print(f'y_pred : {y_pred.shape}')
    y_test = y_test*norm['scale']+norm['shift']
    # calculate metrics in batches    
    test_scores_lead = {}
    # Loop over 12 lead times
    model = lpips.PerceptualLoss(model='net-lin', net='alex', use_gpu=False)#True, gpu_ids=[1])
    for lead in tqdm(range(12)):
        test_scores={}
        if crop > 0: 
            yt = y_test[:,crop:-crop,crop:-crop,lead:lead+1] # truth data
            yp = y_pred[:,crop:-crop,crop:-crop,lead:lead+1] # predictions have been scaled earlier
        else:
            yt = y_test[...,lead:lead+1] # truth data
            yp = y_pred[...,lead:lead+1] # predictions have been scaled earlier
        test_scores['ssim'] = run_metric(ssim, [255], yt, yp, batch_size=32)
        test_scores['mse'] = run_metric(MSE, 255, yt, yp, batch_size=32)
        test_scores['mae'] = run_metric(MAE, 255, yt, yp, batch_size=32)
        test_scores['lpips'] = get_lpips(model,yp,yt,batch_size=32,n_out=1)[0] # because this is scalar
        
        H,rb,cb=run_histogram(yt,yp,bins=range(255))
        thresholds = [16,74,133,160,181,219]
        scores = score_histogram(H,rb,cb,thresholds)
        for t in thresholds:
            test_scores['pod%d' % t] = scores[t]['pod']
            test_scores['sucr%d' % t] = 1-scores[t]['far']
            test_scores['csi%d' % t] = scores[t]['csi']
            test_scores['bias%d' % t] = scores[t]['bias']
        
        test_scores_lead[lead]=test_scores
    
    print(f'saving to : {output_csv_file}')
    df = pd.DataFrame({k:[v] for k,v in test_scores_lead.items()})
    df.to_csv(output_csv_file)
    
    return
    

if __name__=='__main__':
    main()

    
    
    
