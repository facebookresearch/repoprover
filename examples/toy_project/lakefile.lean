-- Copyright (c) Meta Platforms, Inc. and affiliates.
-- All rights reserved.

import Lake
open Lake DSL

package «test» where
  leanOptions := #[
    ⟨`autoImplicit, false⟩
  ]

require mathlib from git
  "https://github.com/leanprover-community/mathlib4" @ "v4.28.0"

require REPL from git
  "https://github.com/leanprover-community/repl" @ "v4.28.0-rc1"

@[default_target]
lean_lib «TestProject» where
  globs := #[.submodules `TestProject]
