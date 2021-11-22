#!/bin/bash 
PARTITION=gpu-2080ti
#FLAGS= 
PYTHONPATH=. srun --job-name="$JOB_NAME" --partition=$PARTITION --cpus-per-task=4 --mem=16G --pty --gres=gpu:2 -- ./scripts/run_singularity_server.sh python3 ./care_nl_ica/cl_causal.py  --project mlp-test --use-batch-norm  --use-dep-mat --use-wandb --n-steps 400001 "$@"
