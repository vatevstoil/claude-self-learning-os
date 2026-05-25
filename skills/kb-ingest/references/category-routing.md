# Category Routing Reference

Category routing maps RAW file content keywords to wiki categories.
Each project overrides this in its `KB_CONFIG.md`.

## Default Routing Table

| Keywords in content | → Category | Cluster key |
|---|---|---|
| TradingView, MCP, CDP, Chrome DevTools | TradingView Setup | `tradingview_setup` |
| BitGet, Railway, bot, execution, exchange API | Automated Trading | `automated_trading` |
| AutoAgent, Harbor, optimization, benchmark | Agent Optimization | `agent_optimization` |
| rules.json, risk, position sizing, stop-loss | Strategy & Risk | `strategy_risk` |
| FreqAI, LightGBM, XGBoost, ML, backtest | Strategy & Risk | `strategy_risk` |
| TAO, Bittensor, subnet, airdrop, wallet, chain | Crypto & Blockchain | `crypto_blockchain` |
| Polymarket, prediction market, Bullpen | Prediction Markets | `prediction_markets` |
| HMM, regime, Alpaca, AlphaInsider | Advanced Bot Architecture | `advanced_bots` |
| stock, fundamental, earnings, balance sheet | Stock Research | `stock_research` |
| tutorial, guide, setup, install | Tutorials | `tutorials` |
| YouTube transcript, video | (use content to determine above) | — |
| GitHub README, docs | (use content to determine above) | — |

## Project-Specific Routing

### Claude Trading (`{{RESEARCH_PATH}}\Claude Trading\`)
Notebook: `15d280af-2689-4ff8-ab6a-096f34415a83`
Categories: TradingView Setup | Automated Trading | Agent Optimization | Strategy & Risk | Crypto & Blockchain | Prediction Markets | Advanced Bot Architecture

### Claude Code Research (`{{RESEARCH_PATH}}\Claude Code Resurch\`)
Notebook: *(read from KB_CONFIG.md)*
Categories: CLI Setup | MCP Servers | Memory Systems | Agent Patterns | Skills | Deployment

### AI Video (`{{RESEARCH_PATH}}\AI Video\`)
Notebook: *(read from KB_CONFIG.md)*
Categories: Video Generation | Image Generation | Prompt Library | Workflows | Tools

## How to Create Project-Specific Routing

In `KB_CONFIG.md` at project base, add:

```markdown
## Category Routing

| Keywords | Category | Cluster |
|---|---|---|
| <keyword1, keyword2> | <Category Name> | <cluster_key> |
```
