/* SPDX-License-Identifier: GPL-2.0-only */
/* Adapted from Wolfson/NXP wm8960.c, copyright 2007-11 Wolfson
 * Microelectronics. Checked arithmetic and I/O by Dreem contributors, 2026.
 */
struct _pll_div {
	u32 pre_div;
	u32 n;
	u32 k;
};

static int pll_factors(unsigned int source, unsigned int target,
		       struct _pll_div *div)
{
	u64 scaled, integer, fraction;
	u32 remainder;

	if (!source || !target || target > UINT_MAX / 8)
		return -EINVAL;
	scaled = (u64)target * 4;
	integer = scaled;
	do_div(integer, source);
	div->pre_div = integer < 6;
	if (div->pre_div)
		scaled *= 2;
	integer = scaled;
	remainder = do_div(integer, source);
	if (integer < 6 || integer > 12)
		return -EINVAL;
	fraction = ((u64)remainder << 24) + source / 2;
	do_div(fraction, source);
	/* Rounding can carry into N; do not silently truncate it to zero. */
	if (fraction == (1U << 24)) {
		integer++;
		fraction = 0;
	}
	if (integer > 12)
		return -EINVAL;
	div->n = integer;
	div->k = fraction;
	return 0;
}

static bool is_pll_freq_available(unsigned int source, unsigned int target)
{
	struct _pll_div div;

	return !pll_factors(source, target, &div);
}

static int wm8960_set_pll(struct snd_soc_codec *codec,
		unsigned int freq_in, unsigned int freq_out)
{
	struct _pll_div div;
	int ret, cleanup;
	u16 reg;

	if (!!freq_in != !!freq_out)
		return -EINVAL;
	if (freq_in) {
		ret = pll_factors(freq_in, freq_out, &div);
		if (ret)
			return ret;
	}
	ret = dreem_wm8960_update_bits(codec, WM8960_CLOCK1, 1, 0);
	if (ret < 0)
		return ret;
	ret = dreem_wm8960_update_bits(codec, WM8960_POWER2, 1, 0);
	if (ret < 0 || !freq_in)
		return ret < 0 ? ret : 0;
	ret = snd_soc_read(codec, WM8960_PLL1);
	if (ret < 0)
		return ret;
	reg = (ret & ~0x3f) | (div.pre_div << 4) | div.n;
	if (div.k)
		reg |= 0x20;
	ret = snd_soc_write(codec, WM8960_PLL2, (div.k >> 16) & 0xff);
	if (ret < 0)
		goto fail;
	ret = snd_soc_write(codec, WM8960_PLL3, (div.k >> 8) & 0xff);
	if (ret < 0)
		goto fail;
	ret = snd_soc_write(codec, WM8960_PLL4, div.k & 0xff);
	if (ret < 0)
		goto fail;
	ret = snd_soc_write(codec, WM8960_PLL1, reg);
	if (ret < 0)
		goto fail;
	ret = dreem_wm8960_update_bits(codec, WM8960_POWER2, 1, 1);
	if (ret < 0)
		goto fail;
	/* Restore the public upstream settling interval before selecting PLL. */
	msleep(250);
	ret = dreem_wm8960_update_bits(codec, WM8960_CLOCK1, 1, 1);
	if (ret >= 0)
		return 0;
fail:
	cleanup = dreem_wm8960_update_bits(codec, WM8960_CLOCK1, 1, 0);
	if (cleanup < 0)
		dev_warn(codec->dev, "PLL clock cleanup failed: %d\n", cleanup);
	cleanup = dreem_wm8960_update_bits(codec, WM8960_POWER2, 1, 0);
	if (cleanup < 0)
		dev_warn(codec->dev, "PLL power cleanup failed: %d\n", cleanup);
	return ret;
}

static int wm8960_set_dai_pll(struct snd_soc_dai *dai, int pll_id,
		int source, unsigned int freq_in, unsigned int freq_out)
{
	struct snd_soc_codec *codec = dai->codec;
	struct wm8960_priv *wm8960 = snd_soc_codec_get_drvdata(codec);
	int ret;

	if (pll_id != WM8960_SYSCLK_PLL && pll_id != WM8960_SYSCLK_AUTO)
		return -EINVAL;
	if (wm8960->is_stream_in_use[0] || wm8960->is_stream_in_use[1])
		return -EBUSY;
	if (pll_id == WM8960_SYSCLK_AUTO) {
		if (!freq_in || freq_in > INT_MAX)
			return -EINVAL;
		wm8960->freq_in = freq_in;
		return 0;
	}
	ret = wm8960_set_pll(codec, freq_in, freq_out);
	if (!ret)
		wm8960->freq_in = freq_in;
	return ret;
}
