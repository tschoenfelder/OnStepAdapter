# OnStepAdapter 0.6.6

Terrestrial DEC jogs now use a continuous northern-GEM Axis-2 coordinate:
`DEC` on the east representation and `180 degrees - DEC` on the west
representation. Crossing the north celestial pole changes the coordinate
representation and shifts RA by 12 hours exactly once; subsequent jogs continue
on the same side without another shift. Non-crossing pier-side changes remain a
hard safety failure.
