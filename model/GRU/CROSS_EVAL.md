# Cross Evaluation

Run the four model/test combinations with:

```bash
python3 model/GRU/run_cross_eval.py
```

Dry run only:

```bash
python3 model/GRU/run_cross_eval.py --dry-run
```

Outputs are written under `runs/gru/cross_eval/`:
- `summary.json`
- one folder per experiment with `sweep.csv`, `metrics.json`, and plots
