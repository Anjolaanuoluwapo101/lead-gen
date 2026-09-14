"""Strands agent package for the AgentCore runtime.

Modules here are deliberately importable WITHOUT the agent's heavier deps
(strands / bedrock_agentcore) so run_store and budget can be unit-tested and
reused by the Flask dashboard on its own.
"""
