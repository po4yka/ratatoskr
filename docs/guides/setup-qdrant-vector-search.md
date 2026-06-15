# Set Up Qdrant Vector Search

Enable semantic search with Qdrant and configurable embedding providers (local sentence-transformers or Google Gemini API). Qdrant stores summary vectors and, when GitHub ingestion is enabled, repository vectors in the same collection with an `entity_type` discriminator.

**Audience:** Operators **Difficulty:** Intermediate **Estimated Time:** 15 minutes

---

## What Qdrant Provides

Qdrant enables **semantic search** over summaries and analyzed GitHub repositories:

- **Natural language queries**: "machine learning tutorials" finds relevant articles even if they use different terms
- **Vector embeddings**: Converts text to vectors using sentence-transformers (384-dim, local) or Gemini Embedding 2 API (768-dim, remote)
- **Similarity search**: Finds semantically similar summaries (not just keyword matches)
- **Hybrid search**: Combines semantic search with full-text search and reranking

**Use case**: Search past summaries and indexed repositories by meaning, not just keywords.

Qdrant is lighter than ChromaDB, ships a first-class arm64 Docker image, and has no `onnxruntime` dependency — making it suitable for Raspberry Pi and Apple Silicon hosts.

---

## Prerequisites

- Ratatoskr installed and running
- **Local provider:** Python 3.13+ with sentence-transformers support, 1-2 GB RAM for embedding model
- **Gemini provider:** Google Gemini API key ([get one free](https://aistudio.google.com/apikey)), `pip install google-genai`

---

## Steps

### 1. Install Qdrant

**Option A: Docker (Recommended)**

```bash
# Start Qdrant container
docker run -d \
  --name qdrant \
  -p 6333:6333 \
  -p 6334:6334 \
  -v $(pwd)/qdrant_data:/qdrant/storage \
  --restart unless-stopped \
  qdrant/qdrant:v1.12.4

# Verify running
curl http://localhost:6333/healthz
# Should return: {"title":"qdrant - vector search engine","version":"..."}
```

**Option B: Docker Compose (recommended for the full stack)**

The `ops/docker/docker-compose.yml` already includes a `qdrant` service. Start it with:

```bash
docker compose -f ops/docker/docker-compose.yml up -d qdrant
```

---

### 2. Configure Connection

Add to your `.env` file:

```bash
# Enable Qdrant
QDRANT_REQUIRED=true

# Qdrant server
QDRANT_URL=http://localhost:6333

# Optional: API key for secured Qdrant instances
# QDRANT_API_KEY=your-api-key

# Environment label for collection namespacing
QDRANT_ENV=dev

# Collection version suffix
QDRANT_COLLECTION_VERSION=v1
```

---

### 3. Download Embedding Model

The embedding model downloads automatically on first use, but you can pre-download:

```bash
# Pre-download model (optional)
python -c "
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('all-MiniLM-L6-v2')
print('Model downloaded successfully')
"

# Model size: ~90 MB
# Location: ~/.cache/torch/sentence_transformers/
```

---

### 4. Backfill Existing Content

```bash
# Backfill existing summaries with the legacy summary path
python -m app.cli.backfill_vector_store

# Or export summaries + analyzed GitHub repositories with CocoIndex
pip install -e ".[cocoindex]"
python -m app.cli.backfill_vector_store --use-cocoindex

# Expected output:
# INFO: Found 150 summaries to backfill
# INFO: Processing batch 1/3 (50 summaries)
# INFO: Processing batch 2/3 (50 summaries)
# INFO: Processing batch 3/3 (50 summaries)
# INFO: Backfill complete: 150 summaries

# Verify collection created
curl http://localhost:6333/collections
# Should show: "summaries" collection

# Check count
curl http://localhost:6333/collections/summaries
# Should show pointsCount matching indexed summaries plus analyzed repositories
```

---

### 5. Restart Bot

```bash
# Docker
docker restart ratatoskr

# Local
python bot.py
```

---

## Verification

### Test Semantic Search

**Via Telegram Bot:**

```
/search machine learning basics
```

**Via CLI:**

```bash
python -m app.cli.search --query "machine learning basics"
```

**Expected output:**

- Returns semantically related summaries (not just keyword matches)
- Results ranked by relevance (semantic similarity + reranking)
- Fast response (~200-500ms for typical collection)

### Verify Qdrant

```bash
# Check collections
curl http://localhost:6333/collections

# Get collection info
curl http://localhost:6333/collections/summaries

# Query collection directly
curl -X POST http://localhost:6333/collections/summaries/points/search \
  -H "Content-Type: application/json" \
  -d '{
    "vector": [0.1, 0.2, ...],
    "limit": 5
  }'
```

---

## Troubleshooting

### Qdrant connection failed

**Symptom:** Warning logs "Failed to connect to Qdrant"

**Solution:**

```bash
# Check if Qdrant is running
curl http://localhost:6333/healthz

# If not running, start it
# Docker:
docker start qdrant

# Via compose:
docker compose -f ops/docker/docker-compose.yml up -d qdrant

# Verify connection settings
grep QDRANT_URL .env
```

---

### Embedding generation errors

**Symptom:** Error "Failed to generate embeddings"

**Local provider causes & solutions:**

1. **Model not downloaded:**

   ```bash
   # Pre-download model
   python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
   ```

2. **Out of memory:**

   ```bash
   # Use smaller model (default, 90 MB)
   EMBEDDING_PROVIDER=local
   # Or reduce batch size in backfill
   python -m app.cli.backfill_vector_store --batch-size=10
   ```

**Gemini provider causes & solutions:**

1. **Missing API key:**

   ```bash
   # Verify GEMINI_API_KEY is set
   grep GEMINI_API_KEY .env
   ```

2. **Missing dependency:**

   ```bash
   pip install google-genai>=1.0.0
   ```

3. **Rate limiting / quota exceeded:**

   Gemini has per-minute and per-day rate limits. Reduce batch sizes for backfill operations or wait and retry.

---

### Collection not found

**Symptom:** Error "Collection 'summaries' does not exist"

**Solution:**

```bash
# Recreate collection and backfill
python -m app.cli.backfill_vector_store

# Verify collection created
curl http://localhost:6333/collections
```

---

### Search returns no results

**Symptom:** Search query returns empty results

**Diagnostics:**

```bash
# Check collection info
curl http://localhost:6333/collections/summaries
# pointsCount should be > 0

# Check database has summaries
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT count(*) FROM summaries;"

# If count mismatch, backfill again
python -m app.cli.backfill_vector_store
```

---

## Advanced Configuration

### Embedding Provider Selection

Ratatoskr supports two embedding providers, controlled by `EMBEDDING_PROVIDER`:

| Provider | Dimensions | Latency | Cost | Multilingual | Setup |
| ---------- | ---------- | --------- | ------ | ------------ | ------- |
| `local` (default) | 384 | ~50ms | Free (CPU/GPU) | Limited | Download model (~90 MB) |
| `gemini` | 768 (configurable 1-3072) | ~200ms | Free tier / $0.20 per 1M tokens | Native | API key only |

**Local provider** (default -- no changes needed):

```bash
EMBEDDING_PROVIDER=local
```

**Gemini Embedding 2 provider:**

```bash
EMBEDDING_PROVIDER=gemini
GEMINI_API_KEY=your-api-key-here
GEMINI_EMBEDDING_MODEL=gemini-embedding-2-preview   # default
GEMINI_EMBEDDING_DIMENSIONS=768                      # 128-3072; 768/1536/3072 recommended
EMBEDDING_MAX_TOKEN_LENGTH=2048                      # Gemini supports up to 8192
```

Gemini uses task-type-aware embeddings automatically: `RETRIEVAL_DOCUMENT` when indexing summaries, `RETRIEVAL_QUERY` when searching. The `google-genai` package is lazily imported and only required when `EMBEDDING_PROVIDER=gemini`. Qdrant collections are automatically namespaced by model + output dimensionality so newer Gemini Embedding 2 indexes do not collide with older embedding spaces.

**Switching providers** requires re-embedding all data (dimensions differ):

```bash
python -m app.cli.backfill_embeddings --force
python -m app.cli.backfill_vector_store --force
```

---

### Local Embedding Model Selection

These options only apply when `EMBEDDING_PROVIDER=local`.

**Small & Fast (Recommended):**

```bash
EMBEDDING_MODEL=all-MiniLM-L6-v2
# Size: 90 MB
# Embedding dim: 384
# Speed: Fast
# Quality: Good
```

**Balanced:**

```bash
EMBEDDING_MODEL=all-mpnet-base-v2
# Size: 420 MB
# Embedding dim: 768
# Speed: Medium
# Quality: Better
```

**Large & Accurate:**

```bash
EMBEDDING_MODEL=all-roberta-large-v1
# Size: 1.4 GB
# Embedding dim: 1024
# Speed: Slow
# Quality: Best
```

---

### HNSW Index Configuration

Qdrant uses HNSW for approximate nearest-neighbor search. Parameters are set on the collection config:

```bash
# HNSW index parameters (advanced, set at collection creation)
QDRANT_HNSW_M=16               # Number of connections per layer
QDRANT_HNSW_EF_CONSTRUCTION=200  # Quality vs speed tradeoff at index time
QDRANT_HNSW_EF=100             # Search-time quality (ef parameter)
```

**Recommendations:**

- **Higher `m`**: Better recall, more memory
- **Higher `ef_construction`**: Slower indexing, better index quality
- **Higher `ef`**: Slower search, better recall

---

### Hybrid Search Configuration

```bash
# Enable hybrid search (semantic + full-text)
ENABLE_HYBRID_SEARCH=true

# Semantic search weight (0-1, default: 0.7)
SEMANTIC_SEARCH_WEIGHT=0.7

# Full-text search weight (0-1, default: 0.3)
FULLTEXT_SEARCH_WEIGHT=0.3

# Enable reranking
ENABLE_RERANKING=true
RERANKING_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
```

---

## Performance Tuning

### Batch Processing

```bash
# Batch size for backfill upsert operations
python -m app.cli.backfill_vector_store --batch-size=50  # Default

# Increase for faster backfill (requires more RAM)
python -m app.cli.backfill_vector_store --batch-size=200

# Decrease if running out of memory
python -m app.cli.backfill_vector_store --batch-size=25
```

---

## Monitoring

### Collection Statistics

```bash
# Get collection info (includes point count and config)
curl http://localhost:6333/collections/summaries

# Check collection metadata (Postgres-side embedding coverage)
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "
  SELECT
    count(*) AS total_summaries,
    count(*) FILTER (WHERE embedding_blob IS NOT NULL) AS with_embeddings,
    round(
      100.0 * count(*) FILTER (WHERE embedding_blob IS NOT NULL)
        / nullif(count(*), 0),
      2
    ) AS coverage_pct
  FROM summary_embeddings;
"
```

### Search Performance

```bash
# Benchmark search speed
time python -m app.cli.search --query "machine learning"

# Should be < 500ms for collections < 10,000 summaries
```

---

## Maintenance

### Re-index Summaries

```bash
# Re-upsert all points (if model changed or collection was recreated)
python -m app.cli.backfill_vector_store --force

# Or use the CocoIndex one-shot path for summaries + analyzed repositories
python -m app.cli.backfill_vector_store --use-cocoindex

# Incremental update (only new summaries)
python -m app.cli.backfill_vector_store
```

### Clean Orphaned Embeddings

```bash
# Remove embeddings for deleted summaries
python -m app.cli.cleanup_embeddings
```

### Backup Qdrant

```bash
# Qdrant native snapshot API
curl -X POST http://localhost:6333/collections/summaries/snapshots
# Returns snapshot name; download it from /collections/{name}/snapshots/{snapshot}

# Or back up the data directory directly (stop Qdrant first for consistency)
docker stop qdrant
tar -C . -czf qdrant_backup_$(date +%Y%m%d).tar.gz qdrant_data
docker start qdrant

# Restore from directory backup
docker stop qdrant
rm -rf qdrant_data
tar -xzf qdrant_backup_YYYYMMDD.tar.gz
docker start qdrant
```

---

## Disable Qdrant (Rollback)

```bash
# Set to false in .env
QDRANT_REQUIRED=false

# Restart bot
docker restart ratatoskr

# Bot falls back to Postgres full-text search only
```

---

## See Also

- [FAQ § Search](../explanation/faq.md#can-i-search-my-summaries)
- [Troubleshooting § Qdrant Issues](../reference/troubleshooting.md)
- [Environment Variables](../reference/environment-variables.md)
- [SPEC.md § Search](../SPEC.md) - Search architecture

---

**Last Updated:** 2026-05-05
