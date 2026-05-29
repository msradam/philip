Output vhs/sre_agent.gif

Set FontSize 14
Set Width 1100
Set Height 760
Set Theme "Catppuccin Mocha"
Set TypingSpeed 25ms
Set PlaybackSpeed 1.0

# Pre-set the Ollama model env var so the recording uses the bigger
# Granite that classifies cleanly. Run from the project root.
Type "OLLAMA_MODEL=ibm/granite4:micro uv run python examples/mast_sre_agent_walker.py"
Sleep 400ms
Enter
Sleep 13s
