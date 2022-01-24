# select the data
import numpy as np
import matplotlib.pyplot as plt
import h5py
import cv2
from utils import *
from tqdm import tqdm
import os
from metrics import *
import argparse
import pydegensac

from skimage.measure import ransac as skransac
from skimage.transform import FundamentalMatrixTransform
import multiprocessing
import sys
from joblib import Parallel, delayed
import PIL
try:
    from third_party.NM_Net_v2 import NMNET22
    import torch
    import kornia.geometry as KG
    import torch.nn.functional as TF
except Exception as e:
    print (e)
    #sys.exit(0)
    pass


def kornia_find_fundamental_wdlt(points1: torch.Tensor,
                                 points2: torch.Tensor,
                                 weights: torch.Tensor,
                                 params) -> torch.Tensor:
    '''Function, which finds homography via iteratively-reweighted
    least squares ToDo: add citation'''
    F = KG.find_fundamental(points1, points2, weights)
    for i in range(params['maxiter']):
        error = KG.epipolar.metrics.symmetrical_epipolar_distance(points1, points2, F)
        error_norm = TF.normalize(1.0 / (error + 1e-5), dim=1, p=params['conf'])
        F = KG.find_fundamental(points1, points2, error_norm)
    error = KG.epipolar.metrics.symmetrical_epipolar_distance(points1, points2, F)
    mask = error <= params['inl_th']
    return F.detach().cpu().numpy().reshape(3,3), mask.detach().cpu().numpy().reshape(-1)

def norm_test_data(xs_initial, w1,h1,w2,h2):
    cx1 = (w1 - 1.0) * 0.5
    cy1 = (h1 - 1.0) * 0.5
    f1 = max(h1 - 1.0, w1 - 1.0)
    scale1 = 1.0 / f1

    T1 = np.zeros((3, 3,))
    T1[0, 0], T1[1, 1], T1[2, 2] = scale1, scale1, 1
    T1[0, 2], T1[1, 2] = -scale1 * cx1, -scale1 * cy1

    cx2 = (w2 - 1.0) * 0.5
    cy2 = (h2 - 1.0) * 0.5
    f2 = max(h2 - 1.0, w2 - 1.0)
    scale2 = 1.0 / f2

    T2 = np.zeros((3, 3,))
    T2[0, 0], T2[1, 1], T2[2, 2] = scale2, scale2, 1
    T2[0, 2], T2[1, 2] = -scale2 * cx2, -scale2 * cy2

    kp1 = (xs_initial[:, :2] - np.asarray([cx1, cy1])) / np.asarray([f1, f1])
    kp2 = (xs_initial[:, 2:] - np.asarray([cx2, cy2])) / np.asarray([f2, f2])

    xs = np.concatenate([kp1, kp2], axis=-1)
    return xs, T1, T2

def get_single_result(ms, m, method, params, w1 = None, h1 = None, w2 = None, h2  = None):
    mask = ms <= params['match_th']
    tentatives = m[mask]
    tentative_idxs = np.arange(len(mask))[mask]
    src_pts = tentatives[:, :2]
    dst_pts = tentatives[:, 2:]
    if tentatives.shape[0] <= 10:
        return np.eye(3), np.array([False] * len(mask))
    if method == 'cv2f':
        F, mask_inl = cv2.findFundamentalMat(src_pts, dst_pts, 
                                                cv2.RANSAC, 
                                                params['inl_th'],
                                                confidence=params['conf'])
    elif method == 'kornia':
        #mask = ms <= params['match_th']
        #tentatives = m[mask]
        #tentative_idxs = np.arange(len(mask))[mask]
        src_pts = m[:, :2]
        dst_pts = m[:, 2:]
        pts1 = torch.from_numpy(src_pts).view(1, -1, 2)
        pts2 = torch.from_numpy(dst_pts).view(1, -1, 2)
        weights = torch.from_numpy(1.0-ms).view(1, -1).pow(params['match_th'])
        weights = TF.normalize(weights, dim=1)
        F, mask_inl = kornia_find_fundamental_wdlt(pts1.float(), pts2.float(), weights.float(), params)
    elif method == 'cv2eimg':
        tent_norm, T1, T2 = norm_test_data(tentatives, w1,h1,w2,h2)
        #print (T1)
        #K1 = compute_T_with_imagesize(w1,h1)
        #K2 = compute_T_with_imagesize(w2,h2)
        #print (K1, K2)
        #src_pts = normalize_keypoints(src_pts, K1)
        #dst_pts = normalize_keypoints(dst_pts, K2)
        #print (src_pts)
        E, mask_inl = cv2.findEssentialMat(tent_norm[:, :2], tent_norm[:, 2:], 
                                           np.eye(3), cv2.RANSAC, 
                                           threshold=params['inl_th'],
                                           prob=params['conf'])
        F = np.matmul(np.matmul(T2.T, E), T1)
    elif method  == 'pyransac':
        F, mask_inl = pydegensac.findFundamentalMatrix(src_pts, dst_pts, 
                                                params['inl_th'],
                                                conf=params['conf'],
                                                max_iters = params['maxiter'],
                                                enable_degeneracy_check=False)
    elif method  == 'degensac':
        F, mask_inl = pydegensac.findFundamentalMatrix(src_pts, dst_pts, 
                                                params['inl_th'],
                                                conf=params['conf'],
                                                max_iters = params['maxiter'],
                                                enable_degeneracy_check=True)
    elif method  == 'sklearn':
        try:
            #print(src_pts.shape, dst_pts.shape)
            F, mask_inl = skransac([src_pts, dst_pts],
                        FundamentalMatrixTransform,
                        min_samples=8,
                        residual_threshold=params['inl_th'],
                        max_trials=params['maxiter'],
                        stop_probability=params['conf'])
            mask_inl = mask_inl.astype(bool).flatten()
            F = F.params
        except Exception as e:
            print ("Fail!", e)
            return np.eye(3), np.array([False] * len(mask))
    else:
        raise ValueError('Unknown method')
    
    final_inliers = np.array([False] * len(mask))
    if F is not None:
        for i, x in enumerate(mask_inl):
            final_inliers[tentative_idxs[i]] = x
    return F, final_inliers

def get_single_result_nmnet(model,ms, m, method, params, w1, h1, w2, h2):
    with torch.no_grad():
        F, mask = model.predict_F(m, w1, h1, w2, h2)
    return F, mask

        
def create_F_submission(IN_DIR,seq,  method, params = {}):
    out_model = {}
    inls = {}
    matches = load_h5(f'{IN_DIR}/{seq}/matches.h5')
    matches_scores = load_h5(f'{IN_DIR}/{seq}/match_conf.h5')
    keys = [k for k in matches.keys()]
    if method == 'nmnet2':
        model = NMNET22('third_party/model.pth')
        img_names = set()
        for k in keys:
            k1,k2 = k.split('-')
            img_names.add(k1)
            img_names.add(k2)
        img_names = list(img_names)
        wh = {}
        for fname in img_names:
            img = PIL.Image.open(f'{IN_DIR}/{seq}/images/{fname}.jpg')
            w,h = img.size
            wh[fname] = (w,h)
        results = [get_single_result_nmnet(model, matches_scores[k], matches[k], method, params, *(wh[k.split('-')[0]]), *(wh[k.split('-')[1]])) for k in tqdm(keys) ]
        for i, k in enumerate(keys):
            v = results[i]
            out_model[k] = v[0]
            inls[k] = v[1]
    elif method == 'cv2eimg':
        img_names = set()
        for k in keys:
            k1,k2 = k.split('-')
            img_names.add(k1)
            img_names.add(k2)
        img_names = list(img_names)
        wh = {}
        for fname in img_names:
            img = PIL.Image.open(f'{IN_DIR}/{seq}/images/{fname}.jpg')
            w,h = img.size
            wh[fname] = (w,h)
        results = Parallel(n_jobs=num_cores)(delayed(get_single_result)(matches_scores[k], matches[k], method, params, *(wh[k.split('-')[0]]), *(wh[k.split('-')[1]]) ) for k in tqdm(keys))
        for i, k in enumerate(keys):
            v = results[i]
            out_model[k] = v[0]
            inls[k] = v[1]
    elif method == 'load_dfe':
        out_model = load_h5(f'4Dmytro/F_dfe_{seq}_submission.h5')
        inls = load_h5(f'4Dmytro/inls_dfe_{seq}_submission.h5')
    elif method == 'load_oanet':
        out_model = load_h5(f'oanet/fundamental_{args.split}/{seq}/F_weighted.h5')
        inls = load_h5(f'oanet/fundamental_{args.split}/{seq}/corr_th.h5')
    elif method == 'load_oanet_degensac':
        out_model = load_h5(f'oanet/fundamental_{args.split}/{seq}/F_post.h5')
        inls = load_h5(f'oanet/fundamental_{args.split}/{seq}/corr_post.h5')
    else:
        results = Parallel(n_jobs=num_cores)(delayed(get_single_result)(matches_scores[k], matches[k], method, params) for k in tqdm(keys))
        for i, k in enumerate(keys):
            v = results[i]
            out_model[k] = v[0]
            inls[k] = v[1]
    return  out_model, inls

def evaluate_results(submission, split = 'val'):
    ang_errors = {}
    DIR = split
    seqs = os.listdir(DIR)
    for seq in seqs:
        matches = load_h5(f'{DIR}/{seq}/matches.h5')
        K1_K2 = load_h5(f'{DIR}/{seq}/K1_K2.h5')
        R = load_h5(f'{DIR}/{seq}/R.h5')
        T = load_h5(f'{DIR}/{seq}/T.h5')
        F_pred, inl_mask = submission[0][seq], submission[1][seq]
        ang_errors[seq] = {}
        for k, m in tqdm(matches.items()):
            if F_pred[k] is None:
                ang_errors[seq][k] = 3.14
                continue
            img_id1 = k.split('-')[0]
            img_id2 = k.split('-')[1]
            K1 = K1_K2[k][0][0]
            K2 = K1_K2[k][0][1]
            try:
                E_cv_from_F = get_E_from_F(F_pred[k], K1, K2)
            except:
                print ("Fail")
                E = np.eye(3)
            R1 = R[img_id1]
            R2 = R[img_id2]
            T1 = T[img_id1]
            T2 = T[img_id2]
            dR = np.dot(R2, R1.T)
            dT = T2 - np.dot(dR, T1)
            pts1 = m[inl_mask[k],:2] # coordinates in image 1
            pts2 = m[inl_mask[k],2:]  # coordinates in image 2
            p1n = normalize_keypoints(pts1, K1)
            p2n = normalize_keypoints(pts2, K2)
            ang_errors[seq][k] = max(eval_essential_matrix(p1n, p2n, E_cv_from_F, dR, dT))
    return ang_errors


def grid_search_hypers_opencv(INL_THs = [0.75, 1.0, 1.5, 2.0, 3.0, 4.0],
                             MATCH_THs = [0.75, 0.8, 0.85, 0.9, 0.95]):
    res = {}
    for inl_th in INL_THs:
        for match_th in MATCH_THs:
            key = f'{inl_th}_{match_th}'
            print (f"inlier_th = {inl_th}, snn_ration = {match_th}")
            cv2_results = create_F_submission_cv2(split = 'val',
                                                inlier_th = inl_th,
                                                match_th = match_th)
            MAEs = evaluate_results(cv2_results, 'val')
            mAA = calc_mAA_FE(MAEs)
            final = 0
            for k,v in mAA.items():
                final+= v / float(len(mAA))
            print (f'Validation mAA = {final}')
            res[key] = final
    max_MAA = 0
    inl_good = 0
    match_good = 0
    for k, v in res.items():
        if max_MAA < v:
            max_MAA = v
            pars = k.split('_')
            match_good = float(pars[1])
            inl_good =  float(pars[0])
    return inl_good, match_good, max_MAA

if __name__ == '__main__':
    supported_methods = ['kornia', 'cv2f','cv2eimg','load_oanet', 'load_oanet_degensac',  'pyransac', 'degensac', 'sklearn', 'load_dfe', 'nmnet2']
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--split",
        default='val',
        type=str,
        help='split to run on. Can be val or test')
    parser.add_argument(
        "--method", default='cv2F', type=str,
        help=f' can be {supported_methods}' )
    parser.add_argument(
        "--inlier_th",
        default=0.75,
        type=float,
        help='inlier threshold. Default is 0.75')
    parser.add_argument(
        "--conf",
        default=0.999,
        type=float,
        help='confidence Default is 0.999')
    parser.add_argument(
        "--maxiter",
        default=100000,
        type=int,
        help='max iter Default is 100000')
    parser.add_argument(
        "--match_th",
        default=0.85,
        type=float,
        help='match filetring th. Default is 0.85')
    
    parser.add_argument(
        "--force",
        default=False,
        type=bool,
        help='Force recompute if exists')
    parser.add_argument(
        "--data_dir",
        default='f_data',
        type=str,
        help='path to the data')
    
    args = parser.parse_args()

    if args.split not in ['val', 'test']:
        raise ValueError('Unknown value for --split')
    
    if args.method.lower() not in supported_methods:
        raise ValueError(f'Unknown value {args.method.lower()} for --method')
    NUM_RUNS = 1
    if args.split == 'test':
        NUM_RUNS = 3
    params = {"maxiter": args.maxiter,
              "inl_th": args.inlier_th,
              "conf": args.conf,
              "match_th": args.match_th
    }
    problem = 'f'
    OUT_DIR = get_output_dir(problem, args.split, args.method, params)
    IN_DIR = os.path.join(args.data_dir, args.split) 
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    num_cores = int(len(os.sched_getaffinity(0)) * 0.9)
    for run in range(NUM_RUNS):
        seqs = os.listdir(IN_DIR)
        for seq in seqs:
            print (f'Working on {seq}')
            out_models_fname = os.path.join(OUT_DIR, f'submission_models_seq_{seq}_run_{run}.h5')
            out_inliers_fname = os.path.join(OUT_DIR, f'submission_inliers_seq_{seq}_run_{run}.h5')
            
            if os.path.isfile(out_models_fname) and not args.force:
                print (f"Submission file {out_models_fname} already exists, skipping")
                continue
            models, inlier_masks = create_F_submission(IN_DIR, seq,
                                    args.method,
                                    params)
            save_h5(models, out_models_fname)
            save_h5(inlier_masks, out_inliers_fname)
    print ('Done!')
