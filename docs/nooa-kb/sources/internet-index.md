# Internet seed URLs for I-lane workers

Generated: 2026-08-15T09:57:34Z

These URLs are seeds — I-lane workers may add more during execution, but
each worker is capped at 8 distinct URLs total (per spec §5).

Note: NOOA (NVIDIA Object-Oriented Agents) was first publicly released in
v0.0.6 (this pin's commit), and external coverage is sparse. Reddit.com was
unreachable from the WebSearch/WebFetch tools used to populate this file at
plan time, so I1 below is best-effort. I-lane workers that have full web
access should fill gaps.

## I1 — Reddit

1. https://www.reddit.com/search/?q=%22NVIDIA+Object-Oriented+Agents%22+OR+%22labs-OO-Agents%22+OR+%22NOOA+framework%22 — Reddit search for NOOA / NVIDIA Object-Oriented Agents (I-lane worker must verify from a reachable Reddit client)
2. https://www.reddit.com/r/MachineLearning/search/?q=NVIDIA+agents+framework&restrict_sr=on — r/MachineLearning subreddit search for NVIDIA agent framework discussions

## I2 — NVIDIA blogs / devblog

1. https://developer.nvidia.com/blog/six-agent-harness-capabilities-for-higher-model-performance/ — Primary NOOA launch blog post by NVIDIA; describes the six harness capabilities and benchmark results
2. https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/ — Companion NVIDIA blog post on OpenShell sandboxing for autonomous agents (referenced from the NOOA launch blog)

## I3 — GitHub issues & discussions on NVIDIA-NeMo/labs-OO-Agents

1. https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues
2. https://github.com/NVIDIA-NeMo/labs-OO-Agents/discussions

## I4 — Third-party blogs & articles

1. https://arxiv.org/abs/2607.20709 — "NVIDIA-labs OO Agents: Native Python Object-Oriented Agents" — primary third-party academic paper (arXiv)
2. https://github.com/NVIDIA/OpenShell — NVIDIA OpenShell sandboxing repo, cited from the NOOA launch blog as the recommended execution environment
