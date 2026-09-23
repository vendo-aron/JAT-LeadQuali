"""The tenant isolation suite: proof that tenant A cannot see, influence or bill tenant B.

Eight axes, one module each, plus ``docs/tenant-isolation.md`` which is what a prospect's
security reviewer reads. The suite is a package under ``tests/`` so that ``pytest`` — and
therefore CI — collects it with everything else rather than as a job somebody has to
remember to add; ``test_suite_is_collected.py`` asserts exactly that.

Everything here runs without a database except the modules marked ``integration``. That is
not a convenience: a property asserted only by a skipped test is not asserted, and this
repository has twice shipped a hole that every test was green through because the only
coverage was behind Docker.
"""
