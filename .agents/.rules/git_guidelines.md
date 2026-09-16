# Git, Code Review, and Deployment Rules

- **Git Commits allowed**: You may run `git commit` to package changes when the
  user asks you to land them.
- **Git Pushes allowed**: You may run `git push` to the current branch's remote
  when the user asks you to push.
- **Prefer reviewable history**: keep commits small and scoped, and keep
  non-committed scratch work visible in the working directory so the user can
  still review diffs side-by-side in the VS Code Source Control view.
- **No Deployments**: Never run `wrangler deploy`, `npm run deploy`, or any other
  deployment command to push code to the production/default environment.
