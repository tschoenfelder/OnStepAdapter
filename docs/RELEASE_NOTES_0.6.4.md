# OnStepAdapter 0.6.4

Terrestrial finite DEC jogs now remain continuous across either celestial pole.
The adapter reflects the reported DEC and shifts RA by 12 hours when required,
while verifying the requested mechanical Axis-2 displacement. Terrestrial moves
use the configured altitude floor; astronomical DEC bounds are reserved for sky
tracking modes.
