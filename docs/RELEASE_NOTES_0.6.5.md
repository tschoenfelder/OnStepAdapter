# OnStepAdapter 0.6.5

Terrestrial DEC coordinate folding can briefly make INDI's secondary
`TELESCOPE_PIER_SIDE` vector disagree with the authoritative OnStep `:GU#`
status. Version 0.6.5 permits that conflict only during the expected coordinate
transition. The authoritative side must remain unchanged, and the two sources
must agree again before a move is accepted as complete.
