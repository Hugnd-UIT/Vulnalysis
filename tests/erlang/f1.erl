-module(f1).
-export([helper/0, intra/0]).
helper() -> ok.
intra() -> helper().
