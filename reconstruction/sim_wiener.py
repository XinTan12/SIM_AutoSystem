from sim_wiener_gpu_emdapp_batchInGroup_batchBetGroup import (
    hessian_sim_wiener_default_options,
    hessian_sim_wiener_refactor_torch,
)

opts = hessian_sim_wiener_default_options()
opts.mode = "3beam"
opts.wavelength_nm = 488
opts.wiener = 2
opts.avg_groups = 1
opts.pixel_size_nm = 65
opts.excitation_na = 1.0
opts.theta_ratio = (1, 1, 1)
opts.otf_path = r"F:\SIM_AutoSystem\reconstruction\488OTF_512.tif"
opts.background_path = r"F:\SIM_AutoSystem\reconstruction\background.tif"
# opts.output_dir = r"D:\sim\pic\submit1"
opts.output_dir = r"F:\SIM_AutoSystem\reconstruction\data"


opts.use_emd = True
opts.use_regress = True
opts.min_r2 = 0.10
opts.fail_on_low_r2 = False
opts.warn_on_low_r2 = True
opts.fill_missing_c6 = False
# opts.dtype = "double"

#opts.use_saved_params = False
opts.use_saved_params = True
opts.estimated_params_path = r"F:\SIM_AutoSystem\reconstruction\image\488_lifeact-gfpPI4P MITO_111613_first9_stack_estimated_params.mat"

opts.preload_raw_stack_to_gpu = True

# 并行组数
opts.recon_group_batch = 1   # 先从 2 开始试
opts.dtype = "single"

# 计时开关
opts.profile_timing = True
opts.save_timing_json = True
# opts.timing_json_path = r"D:\sim\pic\submit1\timing_summary.json"
opts.timing_json_path = r"F:\SIM_AutoSystem\reconstruction\data\timing_summary2.json"

out = hessian_sim_wiener_refactor_torch(
    # r"D:\sim\pic\combined_LR_32bit-3.tif",
    # r"D:\范云龙\智能超分辨\488_lifeact-gfpPI4P MITO_111613\488_lifeact-gfpPI4P MITO_111613.tif",
r"F:\SIM_AutoSystem\reconstruction\image\488_lifeact-gfpPI4P MITO_111613_first9_stack.tif",
    opts=opts,
    device="cuda",
)

out2 = hessian_sim_wiener_refactor_torch(
    # r"D:\sim\pic\combined_LR_32bit-3.tif",
    # r"D:\范云龙\智能超分辨\488_lifeact-gfpPI4P MITO_111613\488_lifeact-gfpPI4P MITO_111613.tif",
r"F:\SIM_AutoSystem\reconstruction\image\488_lifeact-gfpPI4P MITO_111613_first9_stack.tif",
    opts=opts,
    device="cuda",
)

# print(out["paths"])
# print("\n===== timings (ms) =====")
# for k, v in out["timings_ms"].items():
#     print(f"{k}: {v:.3f}")
