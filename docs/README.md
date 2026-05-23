# Ratatoskr Documentation Hub

Welcome to the Ratatoskr documentation. This guide helps you find the right documentation for your needs.

> Note: this directory keeps current, user-facing and engineering reference docs; temporary planning notes and historical implementation reports are removed after completion.

## Documentation freshness

- Last documentation refresh: **2026-05-23**
- This refresh aligns docs with the current summary-contract descriptor registry, paired prompt/schema loading, owner diagnostics service split, vector reconciliation adapter seam, repository-analysis persistence port, Taskiq runtime bundles, LLM provider protocol/factory behavior, social observability/privacy guardrails, and the existing LangGraph / CocoIndex / GitHub ingestion docs.

## Documentation by Audience

### 👤 I'm a User

You want to use Ratatoskr to summarize articles, videos, or mixed-source bundles.

**Start here**:

1. [Quickstart Tutorial](guides/quickstart.md) - Get your first summary in 5 minutes
2. [External Access Quickstart](guides/external-access-quickstart.md) - First CLI or MCP aggregation session
3. [FAQ](explanation/faq.md) - Common questions answered
4. [Deploy to Production](guides/deploy-production.md) - Setup and installation
5. [Troubleshooting](reference/troubleshooting.md) - Fix common issues

**Next steps**:

- [How to enable YouTube support](guides/configure-youtube-download.md)
- [How to enable web search](guides/enable-web-search.md)
- [External Access Quickstart](guides/external-access-quickstart.md)
- [SPEC.md § Data Model](SPEC.md#data-model)
- [Environment variables reference](reference/environment-variables.md)

### 💻 I'm a Developer

You want to contribute code, customize the bot, or understand the architecture.

**Start here**:

1. [Architecture Overview](explanation/architecture-overview.md) - Component diagram, request lifecycle, subsystem index
2. [Local Development Tutorial](guides/local-development.md) - Set up dev environment
3. [Frontend Web Guide](reference/frontend-web.md) - web app architecture and workflows
4. [Architecture Overview § Layering quick reference](explanation/architecture-overview.md#layering-quick-reference) - Why ports and adapters
5. [SPEC.md](SPEC.md) - Technical specification
6. [CLAUDE.md](../CLAUDE.md) - AI assistant guide (comprehensive codebase overview)

**Next steps**:

- [Multi-Agent Architecture](explanation/multi-agent-architecture.md) - LLM pipeline design
- [Explanation docs](README.md#explanation-understanding-oriented) - Design rationale

### 🔧 I'm an Operator

You want to deploy, monitor, and maintain Ratatoskr in production.

**Start here**:

1. [Deploy to Production](guides/deploy-production.md) - Deployment guide
2. [Environment variables reference](reference/environment-variables.md) - Configuration
3. [Troubleshooting](reference/troubleshooting.md) - Debugging

**Next steps**:

- [How to setup Redis caching](guides/setup-redis-caching.md)
- [How to setup Qdrant](guides/setup-qdrant-vector-search.md)
- [CocoIndex vector sync](cocoindex.md)
- [How to backup and restore](guides/backup-and-restore.md)
- [How to optimize performance](guides/optimize-performance.md)

### 🤝 I'm a Contributor

You want to submit pull requests or improve the project.

**Start here**:

1. [Local Development Tutorial](guides/local-development.md)
2. Code standards: See [CLAUDE.md § Code Standards](../CLAUDE.md#code-standards)

**Next steps**:

- [SPEC.md](SPEC.md) - Technical specification
- [Architecture Overview § Layering quick reference](explanation/architecture-overview.md#layering-quick-reference) - Code organization
- [Scraper chain explainer](explanation/scraper-chain.md) - Provider taxonomy, fallback logic, and deployment topology
- [Frontend Web Guide](reference/frontend-web.md) - web app architecture and design shim notes

### 🔌 I'm an Integrator

You want to integrate Ratatoskr with other tools or build a client.

**Start here**:

1. [Mobile API Reference](reference/mobile-api.md) - REST API specification
2. [Frontend Web Guide](reference/frontend-web.md) - Web client routes, auth, and API usage
3. [MCP Server Guide](reference/mcp-server.md) - AI agent integration
4. [External Access Quickstart](guides/external-access-quickstart.md)
5. [First Mobile API Client Tutorial](guides/first-mobile-api-client.md)

**Next steps**:

- [OpenAPI Schema](openapi/) - Machine-readable API spec
- [Database Schema Reference](SPEC.md#database-schema) - Direct database access
- [Summary Contract Reference](reference/summary-contract.md) - JSON output format

---

## Documentation by Task

### 🚀 Getting Started

**I want to...**

- **Get my first summary in 5 minutes** → [Quickstart Tutorial](guides/quickstart.md)
- **Submit an aggregation bundle from CLI or MCP** → [External Access Quickstart](guides/external-access-quickstart.md)
- **Install on my server** → [Deploy to Production](guides/deploy-production.md)
- **Understand what this project does** → [README.md](../README.md) (project root)
- **Decide if this is right for me** → [FAQ](explanation/faq.md)
- **Open the web UI** → [Frontend Web Guide](reference/frontend-web.md)

### 🛠 Configuring Features

**I want to...**

- **Enable YouTube support** → [How to configure YouTube download](guides/configure-youtube-download.md)
- **Enable Twitter / X extraction** → [How to configure Twitter / X extraction](guides/configure-twitter-extraction.md)
- **Upgrade across the project rename** → [Migrate from bite-size-reader](guides/migrate-from-bite-size-reader.md)
- **Run mixed-source aggregation** → [SPEC.md § Data Model](SPEC.md#data-model)
- **Onboard an external CLI or MCP client** → [External Access Quickstart](guides/external-access-quickstart.md)
- **Enable web search enrichment** → [How to enable web search](guides/enable-web-search.md)
- **Setup Redis caching** → [How to setup Redis caching](guides/setup-redis-caching.md)
- **Setup semantic search (Qdrant)** → [How to setup Qdrant](guides/setup-qdrant-vector-search.md)
- **See all config options** → [Environment variables reference](reference/environment-variables.md)

### 🐛 Troubleshooting

**I'm experiencing...**

- **Bot not starting** → [Troubleshooting § Configuration Issues](reference/troubleshooting.md#configuration-issues)
- **Summaries failing** → [Troubleshooting § Scraper/OpenRouter Issues](reference/troubleshooting.md)
- **YouTube downloads failing** → [Troubleshooting § YouTube Issues](reference/troubleshooting.md#youtube-issues)
- **Slow performance** → [Troubleshooting § Performance Issues](reference/troubleshooting.md#performance-issues)
- **Something else** → [Troubleshooting](reference/troubleshooting.md) (full guide)

### 🔍 Understanding the System

**I want to...**

- **Get the high-level picture** → [Architecture Overview](explanation/architecture-overview.md)
- **Understand the layer rationale** → [Architecture Overview § Layering quick reference](explanation/architecture-overview.md#layering-quick-reference)
- **Understand the multi-agent pipeline** → [Multi-Agent Architecture](explanation/multi-agent-architecture.md)
- **Understand design decisions** → [Design Philosophy](explanation/design-philosophy.md)
- **See the full technical spec** → [SPEC.md](SPEC.md)

### 🧑‍💻 Developing

**I want to...**

- **Set up local dev environment** → [Local Development Tutorial](guides/local-development.md)
- **Run web app locally** → [Frontend Web Guide](reference/frontend-web.md#local-development)
- **Run tests** → [Local Development Tutorial § Running Tests](guides/local-development.md)
- **Run web static checks** → [Frontend Web Guide](reference/frontend-web.md#quality-checks)
- **Add a new feature** → [CLAUDE.md § Adding a New Feature](../CLAUDE.md#common-tasks)
- **Understand the codebase** → [CLAUDE.md](../CLAUDE.md) (AI assistant guide, comprehensive)

### 🔌 Integrating

**I want to...**

- **Build a mobile app client** → [First Mobile API Client Tutorial](guides/first-mobile-api-client.md)
- **Connect the packaged CLI or hosted MCP** → [External Access Quickstart](guides/external-access-quickstart.md)
- **Build or extend web client** → [Frontend Web Guide](reference/frontend-web.md)
- **Integrate with Claude Desktop** → [MCP Server Guide](reference/mcp-server.md)
- **Access the database directly** → [SPEC.md § Database Schema](SPEC.md#database-schema)
- **See the full API spec** → [Mobile API Reference](reference/mobile-api.md)

---

## Documentation by Type (Diátaxis Framework)

The documentation is organized using the [Diátaxis framework](https://diataxis.fr/), which categorizes docs into four types:

### Guides (Learning- and Goal-Oriented)

Step-by-step lessons and practical recipes, all in `docs/guides/`.

| Guide | Description | Audience | Time |
| ------- | ------------- | ---------- | ------ |
| [Quickstart](guides/quickstart.md) | Get your first summary in 5 minutes | Users | 5 min |
| [Clone to First Summary](guides/clone-to-first-summary.md) | Minimal clone-to-run steps | Users | 10 min |
| [Local Development](guides/local-development.md) | Full local dev environment setup | Developers | 20 min |
| [First Mobile API Client](guides/first-mobile-api-client.md) | Build a simple mobile client | Integrators | 30 min |
| [External Access Quickstart](guides/external-access-quickstart.md) | First CLI or MCP aggregation session | Integrators, External users | 10 min |
| [Configure YouTube Download](guides/configure-youtube-download.md) | Enable YouTube support | Users, Operators | |
| [Configure Twitter / X Extraction](guides/configure-twitter-extraction.md) | Two-tier (Firecrawl + Playwright) tweet, thread, and X Article extraction | Users, Operators | |
| [Configure Source Ingestors](guides/configure-source-ingestors.md) | Tune the scraper chain providers | Operators | |
| [Deploy to Production](guides/deploy-production.md) | Full production setup with TLS, monitoring, and backups | Operators | |
| [Enable Web Search](guides/enable-web-search.md) | Add real-time web context | Users, Operators | |
| [Setup Redis Caching](guides/setup-redis-caching.md) | Configure Redis | Operators | |
| [Setup Qdrant](guides/setup-qdrant-vector-search.md) | Enable semantic search | Operators | |
| [Optimize Performance](guides/optimize-performance.md) | Tune for speed/cost | Operators | |
| [Backup and Restore](guides/backup-and-restore.md) | Data protection | Operators | |
| [Migrate Versions](guides/migrate-versions.md) | Upgrade between versions | Operators | |
| [Migrate from bite-size-reader](guides/migrate-from-bite-size-reader.md) | Operator checklist for upgrading across the project rename | Operators | |
| [Migrate Telegram Sessions to Telethon](guides/migrate-telegram-sessions-to-telethon.md) | Session migration steps | Operators | |

### Reference (Information-Oriented)

Technical facts, API specs, and complete references.

| Reference | Description | Audience |
| ----------- | ------------- | ---------- |
| [SPEC.md](SPEC.md) | Complete technical specification | Developers, Integrators |
| [Environment Variables](reference/environment-variables.md) | Full configuration reference (250+ vars) | All |
| [Mobile API Reference](reference/mobile-api.md) | REST API specification | Integrators |
| [Frontend Web Guide](reference/frontend-web.md) | web app architecture, auth, and workflows | Developers, Integrators |
| [OpenAPI Schema](openapi/) | Machine-readable API spec | Integrators |
| [Summary Contract](reference/summary-contract.md) | JSON output format (35+ fields) | Developers, Integrators |
| [Database Schema](SPEC.md#database-schema) | Database tables and relationships | Developers, Integrators |
| [API Contracts](reference/api-contracts.md) | API envelope and response contracts | Developers, Integrators |
| [API Error Codes](reference/api-error-codes.md) | API error code catalog | Developers, Integrators |
| [CLI Commands](reference/cli-commands.md) | CLI command reference | Developers, Operators |
| [Optional YAML Config](reference/config-file.md) | Optional YAML configuration reference | Operators |
| [Data Model](reference/data-model.md) | PostgreSQL schema and SQLAlchemy 2.0 model reference | Developers |
| [Digest Subsystem Ops](reference/digest-subsystem-ops.md) | Channel digest operations reference | Operators |
| [Visual Regression](reference/visual-regression.md) | Visual regression testing reference | Developers |

### Explanation (Understanding-Oriented)

Background, context, and "why" discussions.

| Explanation | Description | Audience |
| ------------- | ------------- | ---------- |
| [Architecture Overview](explanation/architecture-overview.md) | Component diagram, request lifecycle, subsystem index | Operators, Developers, Integrators |
| [Hexagonal Architecture](explanation/architecture-overview.md#layering-quick-reference) | Why ports and adapters (see Architecture Overview) | Developers |
| [Multi-Agent Architecture](explanation/multi-agent-architecture.md) | Why specialized agents | Developers |
| [GitHub Repository Ingestion](explanation/github-repository-ingestion.md) | GitHub stars sync, LLM analysis, semantic search | Developers, Integrators |
| [FAQ](explanation/faq.md) | Frequently asked questions | All |
| [Observability Strategy](explanation/observability-strategy.md) | Observability and telemetry strategy | Operators, Developers |
| [MCP Server](reference/mcp-server.md) | AI agent integration explained | Integrators |
| [Claude Code Hooks](reference/claude-code-hooks.md) | Safety hooks explained | Developers |

### Tasks (Planning-Oriented)

Project planning, roadmap, and task tracking.

| Document | Description | Audience |
| ---------- | ------------- | ---------- |
| [Roadmap Priorities](tasks/roadmap-priorities.md) | Project roadmap and priorities | Developers, Operators |

---

## Quick Reference

### Core Documentation Files

| File | Description | When to Read |
| ------ | ------------- | -------------- |
| [README.md](../README.md) | Project overview, quick start | First time using the project |
| [SPEC.md](SPEC.md) | Technical specification | Deep dive into system design |
| [CLAUDE.md](../CLAUDE.md) | AI assistant guide | Comprehensive codebase overview |
| [FAQ](explanation/faq.md) | Frequently asked questions | Quick answers to common questions |
| [Troubleshooting](reference/troubleshooting.md) | Debugging guide | When something goes wrong |
| [Deploy to Production](guides/deploy-production.md) | Setup and deployment | Initial setup, production deploy |
| [Environment Variables](reference/environment-variables.md) | Config reference | Configuring the system |
| [CHANGELOG.md](../CHANGELOG.md) | Version history | Tracking changes over time |

### Specialized Documentation

| File | Description | When to Read |
| ------ | ------------- | -------------- |
| [Mobile API Reference](reference/mobile-api.md) | REST API spec, including aggregation endpoints | Building mobile client |
| [Frontend Web Guide](reference/frontend-web.md) | Web routes/auth/build details | Building or debugging web UI |
| [Architecture Overview § Layering](explanation/architecture-overview.md#layering-quick-reference) | Architecture guide (layering section) | Understanding code structure |
| [multi-agent-architecture.md](explanation/multi-agent-architecture.md) | Multi-agent LLM | Understanding summarization pipeline |
| [mcp-server.md](reference/mcp-server.md) | MCP integration | Integrating with AI agents |
| [claude-code-hooks.md](reference/claude-code-hooks.md) | Safety hooks | Understanding dev workflow |

---

## Glossary

**Quick reference for key terms:**

- **Correlation ID**: Unique identifier (`UUID`) tying together Telegram messages, database requests, API calls, and logs
- **Summary Contract**: Strict JSON schema (35+ fields) that all LLM summaries must follow
- **Firecrawl**: Managed web scraping API used for content extraction
- **OpenRouter**: Multi-model LLM routing service (supports DeepSeek, Qwen, Kimi, GPT-4, Claude, etc.)
- **Hexagonal Architecture**: Design pattern separating core logic from adapters (Telegram, Firecrawl, database)
- **Multi-Agent Pipeline**: LLM architecture with specialized agents (extraction, summarization, validation, web search)
- **MCP Server**: Model Context Protocol server exposing Ratatoskr to AI agents (Claude Desktop, etc.)
- **Qdrant**: Vector database for semantic search
- **Deduplication Hash**: SHA256 of normalized URL to prevent re-processing same article

See the [Architecture Overview](explanation/architecture-overview.md) for an annotated component diagram, the [SPEC.md](SPEC.md) data-model and API contracts, and the [Multi-Agent Architecture](explanation/multi-agent-architecture.md) explanation for the LLM pipeline-specific terms.

---

## Keyword Index

**Search this index to find relevant documentation:**

| Keyword | See Documentation |
| --------- | ------------------- |
| **API integration** | [Mobile API Reference](reference/mobile-api.md), [First Mobile API Client Tutorial](guides/first-mobile-api-client.md) |
| **Architecture** | [Architecture Overview](explanation/architecture-overview.md), [Layering quick reference](explanation/architecture-overview.md#layering-quick-reference) |
| **Backup** | [How to backup and restore](guides/backup-and-restore.md), [Troubleshooting § Database](reference/troubleshooting.md#database-issues) |
| **Qdrant** | [How to setup Qdrant](guides/setup-qdrant-vector-search.md), [Troubleshooting § Qdrant](reference/troubleshooting.md#qdrant-issues) |
| **Configuration** | [Environment Variables](reference/environment-variables.md), [FAQ § Configuration](explanation/faq.md#configuration) |
| **Cost optimization** | [FAQ § Cost Optimization](explanation/faq.md#cost-optimization) |
| **Database** | [SPEC.md § Database Schema](SPEC.md#database-schema), [Troubleshooting § Database](reference/troubleshooting.md#database-issues) |
| **Debugging** | [Troubleshooting](reference/troubleshooting.md), [SPEC.md § Correlation IDs](SPEC.md#correlation-ids) |
| **Deployment** | [Deploy to Production](guides/deploy-production.md), [Quickstart Tutorial](guides/quickstart.md) |
| **Docker** | [Deploy to Production](guides/deploy-production.md), [FAQ § Installation](explanation/faq.md#installation) |
| **Firecrawl** | [Scraper chain explainer](explanation/scraper-chain.md), [Troubleshooting](reference/troubleshooting.md) |
| **Installation** | [Deploy to Production](guides/deploy-production.md), [FAQ § Installation](explanation/faq.md#installation) |
| **LLM providers and models** | [Environment Variables](reference/environment-variables.md), [Optional YAML Configuration](reference/config-file.md), [FAQ § Configuration](explanation/faq.md#can-i-use-openai-instead-of-openrouter), [FAQ § Cost](explanation/faq.md#what-are-the-cheapest-models-that-work-well) |
| **MCP Server** | [reference/mcp-server.md](reference/mcp-server.md), [Troubleshooting § MCP](reference/troubleshooting.md#mcp-server-issues) |
| **Mobile API** | [Mobile API Reference](reference/mobile-api.md), [First Mobile API Client Tutorial](guides/first-mobile-api-client.md) |
| **Mixed-source aggregation** | [SPEC.md](SPEC.md), [Mobile API Reference](reference/mobile-api.md), [Environment Variables](reference/environment-variables.md) |
| **Multi-agent** | [multi-agent-architecture.md](explanation/multi-agent-architecture.md) |
| **OpenRouter** | [Environment Variables](reference/environment-variables.md), [Troubleshooting § OpenRouter](reference/troubleshooting.md#openrouter-issues) |
| **Performance** | [How to optimize performance](guides/optimize-performance.md), [Troubleshooting § Performance](reference/troubleshooting.md#performance-issues) |
| **Redis** | [How to setup Redis](guides/setup-redis-caching.md), [Troubleshooting § Redis](reference/troubleshooting.md#redis-issues) |
| **Search** | [SPEC.md § Search](SPEC.md#search), [How to setup Qdrant](guides/setup-qdrant-vector-search.md) |
| **Security** | [FAQ § Security](explanation/faq.md#security) |
| **Social integrations** | [Social Integrations](reference/social-integrations.md), [Environment Variables](reference/environment-variables.md#social-integrations), [Observability Strategy](explanation/observability-strategy.md#social-integration-metrics) |
| **Summary contract** | [Summary Contract](reference/summary-contract.md), [Summary Contract Design](explanation/summary-contract-design.md) |
| **Testing** | [Local Development Tutorial § Testing](guides/local-development.md), [CLAUDE.md § Testing](../CLAUDE.md#testing) |
| **Troubleshooting** | [Troubleshooting](reference/troubleshooting.md), [FAQ](explanation/faq.md) |
| **Web interface** | [Frontend Web Guide](reference/frontend-web.md), [README.md](../README.md) |
| **Web search** | [How to enable web search](guides/enable-web-search.md), [FAQ § Web Search](explanation/faq.md#web-search) |
| **YouTube** | [How to configure YouTube](guides/configure-youtube-download.md), [Troubleshooting § YouTube](reference/troubleshooting.md#youtube-issues) |

---

## Contributing to Documentation

Found a typo? Documentation unclear? Want to add a tutorial?

1. **Small fixes**: Edit directly and submit PR
2. **New documentation**: Follow [Diátaxis framework](https://diataxis.fr/) - Tutorials = step-by-step lessons - How-to guides = goal-oriented recipes - Reference = technical facts - Explanation = background and "why"
3. **Update this hub**: Add new docs to relevant sections above

---

**Last Updated**: 2026-05-23

**Questions?** Check [FAQ](explanation/faq.md) or open an [issue](https://github.com/po4yka/ratatoskr/issues).
