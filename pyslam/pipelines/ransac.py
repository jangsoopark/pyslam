import random
import numpy as np
from liegroups import SE3, SO3
from numba import guvectorize, float64, boolean
import time

#from pyslam.sensors.stereo_camera import _stereo_project
SE3_SHAPE = np.empty(4)


@guvectorize([(float64[:, :], float64[:, :], float64[:], float64[:, :])],
             '(n,m),(n,m), (p) ->(p,p)', nopython=True, cache=True, target='parallel')
def compute_transform_fast(pts_1, pts_2, dummy, out):
    """Computes SE(3) transformation from a set of rigid 3D point clouds using SVD (See Barfoot's Textbook)"""

    numPts = len(pts_1)

    # This form of np.mean is required for guvectorize (the optional axis parameter is not supported)
    centroid_1 = np.array(
        [np.mean(pts_1[:, 0]), np.mean(pts_1[:, 1]), np.mean(pts_1[:, 2])])
    centroid_2 = np.array(
        [np.mean(pts_2[:, 0]), np.mean(pts_2[:, 1]), np.mean(pts_2[:, 2])])

    pts_1_c = pts_1 - centroid_1
    pts_2_c = pts_2 - centroid_2

    W = (1.0/numPts)*np.dot(pts_2_c.T, pts_1_c)

    U, _, V = np.linalg.svd(W, full_matrices=True)
    S = np.identity(3)
    S[2, 2] = np.linalg.det(U)*np.linalg.det(V)
    C_21 = np.dot(U, np.dot(S, V))

    r_21_1 = np.dot(-C_21.T, centroid_2) + centroid_1
    trans = np.dot(-C_21, r_21_1)

    # Each element must be set individually
    out[0, 0] = C_21[0, 0]
    out[0, 1] = C_21[0, 1]
    out[0, 2] = C_21[0, 2]
    out[0, 3] = trans[0]

    out[1, 0] = C_21[1, 0]
    out[1, 1] = C_21[1, 1]
    out[1, 2] = C_21[1, 2]
    out[1, 3] = trans[1]

    out[2, 0] = C_21[2, 0]
    out[2, 1] = C_21[2, 1]
    out[2, 2] = C_21[2, 2]
    out[2, 3] = trans[2]

    out[3, 0] = 0
    out[3, 1] = 0
    out[3, 2] = 0
    out[3, 3] = 1


@guvectorize([(float64[:, :], float64[:, :], float64[:, :], float64[:], float64[:], boolean[:])],
             '(p,p),(m,n), (m,n), (q), () -> (m)', nopython=True, cache=True, target='parallel')
def compute_ransac_cost_fast(T_21, pts_1, stereo_obs_2, cam_params, inlier_thresh, out):
    """Compute a binary mask of inliers for each transform proposal"""

    num_pts = pts_1.shape[0]
    cu, cv, fu, fv, b = cam_params
    #stereo_obs_2_test = np.empty(stereo_obs_2.shape)

    # NOTE: Numba for optimizes raw python loops much better than 'np.dot', hence this ugly code
    for i in range(num_pts):

        x = T_21[0, 3]
        y = T_21[1, 3]
        z = T_21[2, 3]

        for j in range(3):
            x += T_21[0, j]*pts_1[i, j]
            y += T_21[1, j]*pts_1[i, j]
            z += T_21[2, j]*pts_1[i, j]

        one_over_z = 1. / z
        x_t = fu * x * one_over_z + cu
        y_t = fv * y * one_over_z + cv
        z_t = fu * b * one_over_z

        error = (x_t - stereo_obs_2[i, 0])*(x_t - stereo_obs_2[i, 0]) + (
                (y_t - stereo_obs_2[i, 1])*(y_t - stereo_obs_2[i, 1])) + (
            (z_t - stereo_obs_2[i, 2])*(z_t - stereo_obs_2[i, 2]))

        out[i] = error < inlier_thresh[0]


class FrameToFrameRANSAC(object):
    def __init__(self, camera):
        self.camera = camera
        self.ransac_iters = 400
        self.ransac_thresh = 5  # (1**2 + 1**2 + 1**2)
        self.num_min_set_pts = 3

    def perform_ransac(self):
        """Main RANSAC Routine"""
        max_inliers = 0

        # Select random ids for minimal sets
        rand_ids = np.random.randint(self.num_pts, size=(
            self.ransac_iters, self.num_min_set_pts))

        pts_1_sample_stacked = np.empty(
            [self.ransac_iters, self.num_min_set_pts, 3])
        pts_2_sample_stacked = np.empty(
            [self.ransac_iters, self.num_min_set_pts, 3])
        for ransac_i in range(self.ransac_iters):
            pts_1_sample, pts_2_sample = self.pts_1[rand_ids[ransac_i]
                                                    ], self.pts_2[rand_ids[ransac_i]]
            pts_1_sample_stacked[ransac_i, :, :] = pts_1_sample
            pts_2_sample_stacked[ransac_i, :, :] = pts_2_sample

        # Parallel transform computation
        # Compute transforms in parallel
        #start = time.perf_counter()
        T_21_stacked = compute_transform_fast(
            pts_1_sample_stacked, pts_2_sample_stacked, SE3_SHAPE)
        #end = time.perf_counter()
        #print('comp, transform | {}'.format(end - start))

        cam_params = self.camera.cu, self.camera.cv, self.camera.fu, self.camera.fv, self.camera.b
        inlier_thresh = self.ransac_thresh

        # Parallel cost computation
        #start = time.perf_counter()
        inlier_masks_stacked = compute_ransac_cost_fast(
            T_21_stacked, self.pts_1, self.stereo_obs_2, cam_params, inlier_thresh)
        #end = time.perf_counter()
        #print('comp, masks | {}'.format(end - start))

        #start = time.perf_counter()
        inlier_nums = np.sum(inlier_masks_stacked, axis=1)
        most_inliers_idx = np.argmax(inlier_nums)
        T_21_best = SE3.from_matrix(T_21_stacked[most_inliers_idx, :, :])
        max_inliers = inlier_nums[most_inliers_idx]
        inlier_indices_best = np.where(
            inlier_masks_stacked[most_inliers_idx, :])[0]
        #end = time.perf_counter()
        #print('comp, rest | {}'.format(end - start))

        if max_inliers < 5:
            raise ValueError(
                " RANSAC failed to find more than 5 inliers. Try adjusting the thresholds.")

        print('After {} RANSAC iters, found best transform with {} / {} inliers.'.format(
            self.ransac_iters, max_inliers, self.num_pts))

        stereo_obs_1_inliers = self.stereo_obs_1[inlier_indices_best]
        stereo_obs_2_inliers = self.stereo_obs_2[inlier_indices_best]

        return (T_21_best, stereo_obs_1_inliers, stereo_obs_2_inliers, inlier_indices_best)

    def set_obs(self, stereo_obs_1, stereo_obs_2):
        self.stereo_obs_1 = np.atleast_2d(stereo_obs_1)
        self.stereo_obs_2 = np.atleast_2d(stereo_obs_2)

        self.pts_1 = self.camera.triangulate(self.stereo_obs_1)
        self.pts_2 = self.camera.triangulate(self.stereo_obs_2)

        self.num_pts = len(self.pts_1)
