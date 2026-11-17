# Local models on Nudgy's tasks

Measured on 16 Nov 2026 on a laptop with a 4 GB graphics card (GTX 1050 Ti) and 31 GB of memory, with
made-up tasks: 12 spoken requests that need the model (add, tick off, move, prioritise, add a step, split,
draft, chat), one reply draft and one note split into steps. Re-run with `python3 tools/bench_models.py MODEL…` when adding a model.

| Model | Size | Commands right | Per command | Reply draft | Note → steps | Fits a 4 GB GPU |
| --- | --- | --- | --- | --- | --- | --- |
| qwen3:4b (old default) | 2.5 GB | 10/12 | 3.2 s | good, but 40 s (prints its reasoning) | failed | yes |
| **qwen3.5:4b** (default for 4 GB) | 3.3 GB | 10/12 | 5.0 s | 5.8 s, a little wordy | 7.4 s, 3 steps | yes |
| gemma4:e4b | 6.6 GB | 9/12 | 6.3 s | 5.2 s, short and accurate | 11.5 s, 4 good steps | no (part on CPU) |

All three missed "I printed the poster" (a done said in passing) and "write back to Mia…" (a reply to a
chat); Nudgy now recognises both directly, without the model. qwen3.5:9b, 2b and 0.8b are in the
catalogue by size and the vendor's results, not yet measured here.
