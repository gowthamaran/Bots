# Polymarket LP Rewards Bot

A modular Python 3.11+ Polymarket liquidity-provider bot focused on LP Rewards. It is designed to
be safe-by-default in `paper` mode, simulate expected reward yield before deploying capital, and use
Polymarket's official CLOB client adapter for live orders.

> This is trading infrastructure, not financial advice. LP rewards can be overwhelmed by adverse
> selection, stale-price fills, resolution/news risk, and operational failures. Start in paper mode.

## Mechanics implemented

The scoring engine implements the public Polymarket Liquidity Rewards formulas:

```text
S(v, s) = ((v - s) / v)^2 * b
Q_one = sum(YES bids + NO asks weighted by S)
Q_two = sum(YES asks + NO bids weighted by S)
Q_min = max(min(Q_one, Q_two), max(Q_one / 3, Q_two / 3)) for midpoint in [0.10, 0.90]
Q_min = min(Q_one, Q_two) outside that range
```

Design choices follow the practical LP playbook popularized by @herman_m8: prefer reward-enabled
markets with meaningful reward pools, healthy liquidity/volume, enough time before resolution, sane
midpoint ranges, and no obvious event/legal/news traps. The filters are explicit YAML knobs so they
can be tightened for your own risk tolerance.

## Project layout

```text
polymarket_lp_bot/
  config/       YAML + env config models
  data/         Gamma API and CLOB book fetchers
  scoring/      Exact LP rewards formulas
  strategy/     Quote construction and reward-yield simulation
  execution/    Paper executor and py-clob-client-v2 live adapter
  risk/         Inventory limits and circuit breakers
  monitoring/   Telegram alert extension point
  utils/        Reserved for shared helpers
  main.py       Async per-minute orchestration loop
tests/          Pytest formula tests
config/         Example YAML config
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config/config.yaml.example config/config.yaml
cp .env.example .env
```

Load `.env` however you prefer (shell export, direnv, Docker secrets, etc.). The bot never reads a
private key from YAML and never logs secrets.

## Run in paper mode

`config/config.yaml` defaults to paper trading:

```yaml
strategy:
  mode: "paper"
```

Run one continuous loop:

```bash
polymarket-lp-bot --config config/config.yaml
```

Paper mode fetches markets/books, computes scores, estimates daily reward/yield, and logs the orders
it would place without sending anything to the exchange.

## Switch to live mode

1. Set secrets in the environment only:

```bash
export POLYMARKET_PRIVATE_KEY='...'
export POLYMARKET_FUNDER='0x...'              # if your account/signature type needs it
export POLYMARKET_SIGNATURE_TYPE='1'          # if required by your setup
```

2. Change YAML:

```yaml
strategy:
  mode: "live"
```

3. Start with tiny capital limits and wide safety filters. Live mode uses the `py-clob-client-v2`
adapter in `execution/executor.py`.

## Telegram alerts

Assuming you already created a Telegram bot and know the chat ID:

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='...'
```

```yaml
telegram:
  enabled: true
```

The current implementation sends startup, order-cycle, and circuit-breaker alerts. Command handling
(`/status`, `/pause`, `/resume`) is intentionally left as an extension point in `monitoring/`.

## Testing

```bash
pytest
```

The unit tests assert exact formula behavior for the quadratic order score, Q_one/Q_two mapping,
min incentive size filtering, and the 10c/90c Q_min boundary.

## Extension TODOs

- Replace the v1 midpoint approximation with any official size-cutoff-adjusted midpoint endpoint if
  exposed by the CLOB API.
- Competitor-order attribution from the book/user endpoints for more precise reward-share estimates.
- Dynamic spread tightening based on competition, realized volatility, and fill toxicity.
- Multi-market optimizer that allocates capital by marginal Q_min per USDC.
- Historical storage and a dashboard comparing expected vs. paid rewards.
- Backtesting harness and news/event risk filter integrations.
