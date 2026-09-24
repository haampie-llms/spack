import spack.context

ctx = spack.context.default()
KEY = "concretizer:unify"

before = ctx.config.get(KEY)
with ctx.environment.manifest.use_config(ctx):
    within = ctx.config.get(KEY)
after = ctx.config.get(KEY)

if before == within == after:
    print(f"SUCCESS: {before}")
else:
    print(f"FAILURE: {before} -> {within} -> {after}")
