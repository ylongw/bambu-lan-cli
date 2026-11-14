# SPDX-License-Identifier: AGPL-3.0-or-later
class CliError(Exception):
    """Locally generated error safe for display after redaction."""


class AckTimeout(CliError):
    pass
