# ``ctx`` is the context of the ``spack python`` invocation
KEY = "concretizer:unify"

before = ctx.config.get(KEY)  # noqa: F821
with ctx.environment.manifest.use_config(ctx):  # noqa: F821
    within = ctx.config.get(KEY)  # noqa: F821
after = ctx.config.get(KEY)  # noqa: F821

if before == within == after:
    print(f"SUCCESS: {before}")
else:
    print(f"FAILURE: {before} -> {within} -> {after}")
