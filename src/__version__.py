# Single source of truth for the app version shown in the footer and
# GET /api/version. Bump this in the same commit as the change it
# describes, BEFORE tagging the release (git tag vX.Y.Z, gh release
# create) -- never after, so the constant and the tag never drift.
__version__ = "1.5.2"
