# npm

## packages
npm install                               # Install all dependencies from package.json
npm install <package>                     # Add a dependency
npm install -D <package>                  # Add a dev dependency
npm install -g <package>                  # Install a package globally
npm uninstall <package>                   # Remove a dependency
npm update                                # Update dependencies within their semver ranges
npm outdated                              # List outdated dependencies
npm ci                                    # Clean install from package-lock.json (CI)

## scripts
npm run <script>                          # Run a script defined in package.json
npm start                                 # Run the "start" script
npm test                                  # Run the "test" script
npm run build                             # Run the "build" script

## info
npm list                                  # List installed dependencies as a tree
npm list -g --depth=0                     # List top-level global packages
npm view <package>                        # Show registry info about a package
npm audit                                 # Check dependencies for known vulnerabilities
npm audit fix                             # Auto-fix vulnerabilities where possible

## publishing
npm login                                 # Log in to the npm registry
npm version patch                         # Bump patch version and create a git tag
npm publish                               # Publish the package to the registry

## misc
npx <tool>                                # Run a package binary without installing it globally
npm init -y                               # Create a package.json with defaults
npm cache clean --force                   # Clear npm's local cache
