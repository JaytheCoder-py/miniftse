# Module memos

One page per module, each written for a specific non-technical reader.

These exist because index work is half communication: a rule change, a reconstitution surprise or a failed calculation has to be explained to a trustee, a governance committee or an analyst on the rota, in their language and without hedging. Each memo names its reader at the top and is written to them.

| Module | Memo | Written for |
|---|---|---|
| M2 | [Why our index level moved 30bp when nothing traded](M2_why_our_index_level_moved_30bp_when_nothing_traded.md) | a client relationship manager who needs to answer this today |
| M3 | [Why did the index turn over 4.2% in June](M3_why_did_the_index_turn_over_4.2%_in_june.md) | a pension trustee who pays the trading costs |
| M5 | [What evidence would convince us to launch a new factor index](M5_what_evidence_would_convince_us_to_launch_a_new_factor_index.md) | the index governance committee |
| M6 | [Three roads to a value index](M6_three_roads_to_a_value_index.md) | a UK pension fund evaluating factor exposure |
| M8 | [What a constrained optimisation actually decides](M8_what_a_constrained_optimisation_actually_decides.md) | a product manager scoping a climate index |
| M10 | [Why an index provider needs software engineering, not scripts](M10_why_an_index_provider_needs_software_engineering_not_scripts.md) | an engineering manager assessing the platform |
| M12 | [What we do when the 6am calculation fails](M12_what_we_do_when_the_6am_calculation_fails.md) | a new joiner on the operations rota |
| M13 | [Where AI belongs in index research, and where it does not](M13_where_ai_belongs_in_index_research_and_where_it_does_not.md) | the head of index research |
| M15 | [Why changing a rule requires a public consultation](M15_why_changing_a_rule_requires_a_public_consultation.md) | a new analyst who thinks this is bureaucracy |

> Module 1 has no memo here. What it would have covered — why a free price API cannot supply historical market capitalisation, and what that does to a backtest — is documented where the code has to live with it: the `YahooProvider` and `ISharesProvider` docstrings in `data/vendors.py`, and `data/real.py`'s account of the adjusted-close problem.
