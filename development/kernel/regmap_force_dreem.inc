/* SPDX-License-Identifier: GPL-2.0-only */
/* Adapted from regmap.c, copyright 2011 Wolfson Microelectronics.
 * Forced cached-register writes by Dreem research contributors, 2026.
 * Internal built-in audio helper; not exported as a module API.
 */
int dreem_regmap_write_bits(struct regmap *map, unsigned int reg,
			   unsigned int mask, unsigned int value)
{
	unsigned int old, next;
	int ret;

	map->lock(map->lock_arg);
	ret = _regmap_read(map, reg, &old);
	if (!ret) {
		next = (old & ~mask) | (value & mask);
		/* A failed bus write can already have updated the cache. Always
		 * issue this write, even when the cached bits are unchanged.
		 */
		ret = _regmap_write(map, reg, next);
		if (!ret)
			ret = next != old;
	}
	map->unlock(map->lock_arg);
	return ret;
}
