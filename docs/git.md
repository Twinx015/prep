# git

## branch
git branch                                # List local branches
git branch <name>                         # Create a new branch
git branch -d <name>                      # Delete a branch (safe)
git branch -D <name>                      # Delete a branch (force)
git checkout -b <name>                    # Create and switch to a new branch
git switch <name>                         # Switch to an existing branch

## commit
git commit -m "message"                   # Commit staged changes
git commit --amend                        # Edit the last commit
git commit -a -m "message"                # Stage tracked files and commit

## undo
git reset --soft HEAD~1                   # Undo last commit, keep changes staged
git reset --hard HEAD~1                   # Undo last commit, discard changes
git checkout -- <file>                    # Discard changes in a file
git revert <commit>                       # Create a new commit that undoes one

## remote
git remote add origin <url>               # Add a remote
git push -u origin <branch>               # Push and set upstream
git fetch --all                           # Fetch all remotes
git pull --rebase                         # Pull and rebase instead of merge

## stash
git stash                                 # Stash changes
git stash pop                             # Reapply and drop the latest stash
git stash list                            # List all stashes
