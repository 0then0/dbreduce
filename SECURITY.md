# Security policy

## Supported versions

Security fixes target the latest published DBReduce release. Older releases do
not have a separate maintenance branch. Check [PyPI](https://pypi.org/project/dbreduce/)
and the [changelog](CHANGELOG.md) for the current release and upgrade information.
The tested Python/PostgreSQL range is documented in [compatibility](docs/compatibility.md).

## Reporting a vulnerability

Do not publish exploit details, credentials, private database contents or raw
oracle output in public issues or pull requests.

Use **Report a vulnerability** on the repository's
[Security page](https://github.com/0then0/dbreduce/security) when available.
If private reporting is unavailable, open an issue asking @0then0 for a private
security contact, without disclosing the vulnerability or affected private data.

Include the DBReduce version, operating system, Python and PostgreSQL/client
versions, the affected command/backend, impact, and a minimal reproduction using
synthetic data. Describe expected and actual behavior and any known workaround.
Coordinate public disclosure with the maintainer while the report is investigated.
There is no guaranteed response or fix deadline.

## Trust boundaries

DBReduce reads the source database and mutates its own disposable databases.
Oracle commands are user-supplied executables, not sandboxed code. Dumps, triggers,
functions and application services can have effects outside the database copy.
Use trusted inputs and an isolated application environment without production
credentials. See [isolation](docs/isolation.md) and the [guide](docs/guide.md).

Unexpected source writes, mutation or deletion of a database that DBReduce does
not own, disclosure of credentials, or acceptance of an unrelated failure can
warrant a security report. Ordinary application failures and unsupported schema
shapes can be reported through the bug template with sanitized information.
