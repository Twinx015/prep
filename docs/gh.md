# gh

## repo
gh repo clone owner/repo                  # Clone a repo
gh repo create [name]                     # interactive or flags: --public/--private
gh repo fork [repo]                       # Fork a repo, default is current dir's repo
gh repo view [repo]                       # View repo details (--web opens browser)
gh repo list [owner]                      # List repos for a user or org
gh repo delete [repo]                     # Delete a repo (asks to confirm)
gh repo sync                              # Sync a fork with its upstream

gh repo create my-project --public --source=. --remote=origin --push # use after commiting change and making a new repo

## pr
gh pr create                              # Create a pull request (interactive)
gh pr list                                # List open pull requests
gh pr view [number]                       # View a pull request (--web opens browser)
gh pr checkout <number>                   # Checkout a PR branch locally
gh pr merge [number]                      # Merge a pull request
gh pr diff [number]                       # Show the diff for a PR
gh pr review [number]                     # Approve, comment, or request changes

## issue
gh issue create                           # Create a new issue (interactive)
gh issue list                             # List issues
gh issue view <number>                    # View an issue
gh issue close <number>                   # Close an issue
gh issue comment <number>                 # Add a comment to an issue

## auth
gh auth login                             # Authenticate with GitHub
gh auth status                            # Check authentication status
gh auth logout                            # Log out of GitHub
gh auth token                             # Print the current auth token
