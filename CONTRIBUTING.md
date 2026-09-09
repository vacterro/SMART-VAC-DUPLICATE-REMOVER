# Contributing to SMART VAC DUPLICATE REMOVER

Contributions are welcome! Please follow these guidelines:

## Reporting Issues
- Use GitHub Issues for bug reports and feature requests
- Include steps to reproduce bugs
- Describe your environment (OS, Python version)

## Pull Requests
- Fork the repository
- Create a feature branch
- Make your changes
- Test thoroughly
- Submit a pull request with a clear description

## Release process (SHIP-order, T-44)

Version-surface files (`VERSION`, `CHANGELOG.md`, `README.md`, `README.ee.md`,
`README.ru.md`, `README.uk.md`, `README.ja.md`, `README.ded.md`) MUST land in
`BUILD` before `VERIFY`. No edit to these files may land after `REVIEW` has
approved `SHIP` without a new `VERIFY` + `REVIEW` pass. If a version-surface
change is needed in `SHIP`, transition back to `BUILD` and re-run `VERIFY`
and `REVIEW`. This prevents the E-279 (`REVIEW`:`SHIP`) -> E-282
(`BUILD`-followup inside `SHIP`) gap.

## Code Style
- Follow existing code style
- Add comments for complex logic
- Test on Windows if applicable

## License
By contributing, you agree that your contributions will be licensed under the MIT License.