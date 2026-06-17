# Content adapter package: URL processing pipeline from extraction through LLM summarization.
#
# Subdirectories: scraper/ (multi-provider chain), platform_extraction/ (YouTube, Twitter).
# Entry point: graph_url_processor.py (the GraphURLProcessor facade drives the summarize
# graph -- the sole URL summarize path post-T9). To add a summarization feature, extend the
# relevant graph node under app/application/graphs/summarize/nodes/.
