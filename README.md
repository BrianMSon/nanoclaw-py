<div align="center">

<img src="https://raw.githubusercontent.com/gavrielc/nanoclaw/main/assets/nanoclaw-logo.png" alt="NanoClaw" width="200"/>

# nanoclaw-py

**Build Your Personal AI Agent in ~500 Lines of Python**

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Claude Code SDK](https://img.shields.io/badge/Claude_Code_SDK-Anthropic-cc785c?style=flat-square&logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code-sdk)
[![Telegram Bot API](https://img.shields.io/badge/Telegram_Bot_API-v22-26A5E4?style=flat-square&logo=telegram&logoColor=white)](https://python-telegram-bot.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/ApeCodeAI/nanoclaw-py?style=flat-square&logo=github)](https://github.com/ApeCodeAI/nanoclaw-py/stargazers)

<p>
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-features">Features</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#%EF%B8%8F-configuration">Configuration</a> •
  <a href="#-faq">FAQ</a> •
  <a href="#-中文文档">中文</a>
</p>

</div>

---

## What is this?

**nanoclaw-py** is a personal AI Agent powered by the **Claude Code SDK**, communicating with you via **Telegram**. It can read/write files, execute commands, search the web, and schedule tasks — all in **~500 lines of Python**.

nanoclaw-py is its Python rewrite with Telegram as the messaging channel (instead of WhatsApp) and a focus on simplicity. This project is part of the [ApeCode.ai](https://apecode.ai) learning series.
> This project is heavily inspired by [**nanoclaw**](https://github.com/gavrielc/nanoclaw) — a brilliant, minimal Claude agent by [@gavrielc](https://github.com/gavrielc). 

```
You: @Ape Write a Python script to scrape Hacker News headlines
Ape: Sure, let me create that script...
     ✅ Created workspace/hn_scraper.py

You: @Ape Run it every day at 9am and send me the results
Ape: ✅ Scheduled task created (cron: 0 9 * * *)
     I'll run the script and send you results daily at 9:00
```

### Data Flow

<div align="center">
<img src="./assets/architecture.svg" alt="Architecture Diagram" width="800"/>
</div>

## ✨ Features

| Capability | Description |
|------------|-------------|
| **Natural Language** | Powered by Claude Code SDK, understands complex instructions |
| **File Operations** | Read, write, and edit files in the workspace |
| **Command Execution** | Run Bash commands and Python scripts |
| **Web Search** | Built-in WebSearch / WebFetch tools |
| **Task Scheduling** | Cron / interval / one-time tasks with proactive notifications |
| **Long-term Memory** | CLAUDE.md persists user preferences across sessions |
| **Conversation History** | Daily archives in `conversations/` folder, searchable by Agent |
| **Session Continuity** | Auto-restore conversation context after restart |

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Anthropic API Key

### 1. Clone the repo

```bash
git clone https://github.com/BrianMSon/nanoclaw-py.git
cd nanoclaw-py
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token      # From @BotFather
OWNER_ID=your_telegram_user_id          # From @userinfobot
ANTHROPIC_API_KEY=sk-ant-api03-...      # Your Anthropic API Key
```

> **Need an Anthropic API Key?** You can get one at [moacode.org](https://moacode.org/register?ref=bbruceyu), which provides Anthropic API access with a friendly interface.

> **Note:** The `ANTHROPIC_API_KEY` can be left empty — the bot will still work fine without it.

### 4. Run

```bash
uv run python -m nanoclaw
```

Open Telegram, send a message to your bot, and start chatting!

## 🏗 Architecture

```
src/nanoclaw/           533 lines of code (9 files)
├── __main__.py          52 lines  ← Entry point
├── config.py            50 lines  ← Environment variables & paths
├── db.py               114 lines  ← SQLite async operations (tasks only)
├── memory.py            44 lines  ← CLAUDE.md long-term memory
├── agent.py            210 lines  ← Claude Code SDK + 6 MCP tools
├── scheduler.py         75 lines  ← APScheduler task execution
├── bot.py               69 lines  ← Telegram Bot handlers
└── conversations.py     69 lines  ← Daily conversation archiving
```

### MCP Tools

The Agent interacts with Telegram and the scheduler through these MCP tools:

| Tool | Purpose |
|------|---------|
| `send_message` | Proactively send messages (during tasks/long operations) |
| `schedule_task` | Create scheduled tasks (cron/interval/once) |
| `list_tasks` | List all scheduled tasks |
| `pause_task` | Pause a task |
| `resume_task` | Resume a paused task |
| `cancel_task` | Delete a task |

## ⚙️ Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Telegram Bot Token |
| `OWNER_ID` | ✅ | — | Your Telegram user ID |
| `ANTHROPIC_API_KEY` | ✅ | — | Anthropic API Key |
| `ANTHROPIC_BASE_URL` | — | Official | Custom API endpoint (proxy/gateway) |
| `ASSISTANT_NAME` | — | `Ape` | Assistant's name |
| `SCHEDULER_INTERVAL` | — | `60` | Task check interval (seconds) |

> **Custom API Endpoint**: Set `ANTHROPIC_BASE_URL` to route requests through LiteLLM proxy, enterprise gateway, or any Anthropic Messages API compatible endpoint.

## 📂 Data Directories

| Directory | Purpose | Persistent |
|-----------|---------|------------|
| `workspace/` | Agent's working directory for file operations | ✅ |
| `workspace/CLAUDE.md` | Long-term memory (preferences, facts) | ✅ |
| `workspace/conversations/` | Daily chat archives (YYYY-MM-DD.md) | ✅ |
| `store/nanoclaw.db` | SQLite database (scheduled tasks only) | ✅ |
| `data/state.json` | Session ID for conversation continuity | ✅ |

## 🤖 Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message |
| `/clear` | Clear current session, start fresh |
| Any text | Chat with the AI |

## ❓ FAQ

<details>
<summary><b>How do I get my Telegram user ID?</b></summary>

Search for `@userinfobot` on Telegram and send any message. It will reply with your user ID.

</details>

<details>
<summary><b>Why single-user only?</b></summary>

This is an educational project. The `OWNER_ID` restriction ensures only you can use it. The Agent has Bash execution privileges — exposing it publicly would be a security risk.

</details>

<details>
<summary><b>Does the session persist after restart?</b></summary>

Yes. Session is persisted via `session_id` in `data/state.json` and auto-restored on restart. Long-term memory in `workspace/CLAUDE.md` survives even `/clear` commands.

</details>

<details>
<summary><b>Do scheduled tasks survive restart?</b></summary>

Yes. Tasks are stored in SQLite and the scheduler automatically picks up due tasks after restart.

</details>

<details>
<summary><b>Can I use a proxy or custom API endpoint?</b></summary>

Yes. Set `ANTHROPIC_BASE_URL` to your proxy address. It must be compatible with the Anthropic Messages API format.

</details>

## 🔒 Security Notice

> **This project is for personal learning and private deployment only.** The Agent can execute arbitrary commands (`bypassPermissions` mode). Do NOT deploy publicly or share access with others.

## 📄 License

[MIT](LICENSE)

---

