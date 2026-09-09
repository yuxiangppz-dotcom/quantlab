# QuantLab Daily v1 User Guide

QuantLab Daily is a local research and assisted-decision tool. It does not send
orders, move cash, or infer fills from daily bars.

## Start and inspect

From WSL in `/home/administrator/projects/quantlab`:

```bash
uv run quantlab doctor
uv run quantlab ui
```

From Windows, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_quantlab_ui.ps1
```

The UI is local-only at `http://127.0.0.1:8501`. Refreshing it does not call a
provider, train a model, generate a report, or import a fill.

## Daily workflow

1. Inspect the common complete data date on the first page.
2. Explicitly run `uv run quantlab update` when an update is needed. This is the
   only normal Daily command that calls Tushare and writes Canonical data.
3. Click **生成/刷新日频报告**, or run `uv run quantlab daily`.
4. Review data date, model status, risks, ranking, factors and research targets.
5. Optionally create a new immutable configuration in the UI. The baseline and
   transparent-combination candidate are labelled separately; changing count,
   cap or boards creates a new version and does not overwrite the frozen result.

## Account and plan

Download the account template from the fourth page. It is a complete snapshot:
all rows repeat account id/mode/time/cash/open-order declaration, with one
position per row. Cash and reference cost allow at most two CNY decimals;
quantity and sellable quantity are integers.

Use `manual_tracking` for your own complete snapshot. Use `demo_simulation` only
for a clearly labelled demonstration. After import, click **生成下一交易日参考计划**.
The output is BUY/SELL/HOLD/NO_TRADE reference action—not an order. Review the
raw-close date, actual sellable quantity, estimated partial commission and every
pending check.

## Manual fills

Download the fill template. Each actual broker fill needs a stable broker trade
id, trade date, timezone-aware report time, instrument, BUY/SELL, quantity,
Decimal price, explicit gross amount, and actual fee.

Always click **预览并校验成交** first. The preview writes nothing. Only **确认导入已预览成交**
commits the complete batch. Re-importing the same fill is a no-op; changed values
under the same trade id are rejected. The page then shows replayed cash,
positions, fees, and plan-versus-fill differences.

## Acceptance and boundaries

```bash
uv run quantlab accept --account-id demo_200k
```

The acceptance artifact checks the local user path. Product readiness is not a
strategy-profitability claim: the baseline remains test-observed and inferior
to its equal-weight control, the candidate is unpromoted, execution readiness
is false, and no broker gateway exists. See `docs/known_limitations.md` before
using any reference plan.
