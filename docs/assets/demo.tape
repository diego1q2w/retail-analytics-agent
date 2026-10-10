# VHS script for the README demo: https://github.com/charmbracelet/vhs
#
# Record from the repository root with the backend running (./scripts/dev.sh):
#   vhs docs/assets/demo.tape
#
# The conversation runs against live BigQuery and Gemini, so figures and
# wording differ per recording. Long model waits are hidden (Hide/Show) after
# a few seconds of visible progress so the GIF stays short.

Output docs/assets/demo.gif

Set Shell zsh
Set FontSize 15
Set Width 1200
Set Height 720
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Framerate 20
Set WaitTimeout 240s

Hide
Type "./scripts/local_cli.sh"
Enter
Wait+Line /you> *$/
Show
Sleep 1.5s

# 1. A figure question: one guarded query, answer with a cited source.
Type "What's the latest revenue of September?"
Sleep 500ms
Enter
Sleep 6s
Hide
Wait+Line /you> *$/
Show
Sleep 6s

# 2. A follow-up that reuses context.
Type "And August?"
Sleep 500ms
Enter
Sleep 4s
Hide
Wait+Line /you> *$/
Show
Sleep 5s

# 3. A report with recommended actions (takes one to two minutes; hidden).
Type "Write a short report comparing August and September revenue, with recommended actions, and save it."
Sleep 500ms
Enter
Sleep 8s
Hide
Wait+Line /you> *$/
Show
Sleep 7s

# 4. Reports are listed and read by command.
Type "/reports"
Enter
Sleep 4s

# 5. Deletion is only proposed by the assistant: the CLI shows the proposal
#    and nothing is deleted until the user confirms with the exact phrase.
#    A bare /confirm picks the only pending proposal.
Type "Delete the report you just saved."
Sleep 500ms
Enter
Sleep 5s
Hide
Wait+Line /you> *$/
Show
Sleep 6s
Type "/confirm"
Enter
Wait+Line /keep them: *$/
Sleep 2s
Type "delete 1 report"
Sleep 800ms
Enter
Sleep 5s

Type "/quit"
Enter
Sleep 1s
