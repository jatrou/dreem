/* SPDX-License-Identifier: GPL-2.0-only */
/* Clock and PCM setup adapted from Wolfson/NXP wm8960.c (2007-11 Wolfson
 * Microelectronics), with planning/error-state repairs by Dreem contributors,
 * copyright 2026. Requires the public divider tables above this include.
 */
static const unsigned int dreem_dclk_divs[] = { 15, 20, 30, 40, 60, 80, 120, 160 };
static const unsigned int dreem_wm8960_rates[] = {
	8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000,
};
static const struct snd_pcm_hw_constraint_list dreem_wm8960_rate_constraints = {
	.count = ARRAY_SIZE(dreem_wm8960_rates),
	.list = dreem_wm8960_rates,
};

static int wm8960_startup(struct snd_pcm_substream *substream,
			  struct snd_soc_dai *dai)
{
	return snd_pcm_hw_constraint_list(substream->runtime, 0,
			SNDRV_PCM_HW_PARAM_RATE, &dreem_wm8960_rate_constraints);
}

static int wm8960_configure_clocking(struct snd_soc_codec *codec)
{
	struct wm8960_priv *wm8960 = snd_soc_codec_get_drvdata(codec);
	unsigned int input, output = 0, sysclk, rate = wm8960->lrclk;
	unsigned int bclk = wm8960->bclk;
	bool use_pll = false;
	int i, j, k, dclk, pass, ret;

	ret = snd_soc_read(codec, WM8960_IFACE1);
	if (ret < 0)
		return ret;
	/* The board's CPU-master variant retains its external-clock policy. */
	if (!(ret & (1 << 6)))
		return 0;
	if (!rate || rate > 48000 || !bclk || bclk > rate * 64)
		return -EINVAL;
	input = wm8960->clk_id == WM8960_SYSCLK_AUTO ?
		wm8960->freq_in : wm8960->sysclk;
	if (!input || input > INT_MAX)
		return -EINVAL;
	/* First try the supplied clock. AUTO may then choose a PLL whose
	 * internal VCO lies in the manufacturer's preferred 90-100 MHz band.
	 * Solve the whole plan before changing any hardware.
	 */
	for (pass = 0; pass < (wm8960->clk_id == WM8960_SYSCLK_AUTO ? 2 : 1); pass++) {
		for (i = 0; i < ARRAY_SIZE(sysclk_divs); i++) {
			if (sysclk_divs[i] < 0)
				continue;
			for (j = 0; j < ARRAY_SIZE(dac_divs); j++) {
				sysclk = rate * dac_divs[j];
				if (sysclk > 16000015)
					continue;
				output = sysclk * sysclk_divs[i];
				if (!pass && output != input)
					continue;
				if (pass && (output < 22500000 || output > 25000000 ||
					     !is_pll_freq_available(input, output)))
					continue;
				for (k = 0; k < ARRAY_SIZE(bclk_divs); k++) {
					if (sysclk * 10 != bclk * bclk_divs[k])
						continue;
					for (dclk = 0; dclk < ARRAY_SIZE(dreem_dclk_divs); dclk++) {
						if (sysclk * 10 < 700000 * dreem_dclk_divs[dclk] ||
						    sysclk * 10 > 800000 * dreem_dclk_divs[dclk])
							continue;
						use_pll = pass;
						goto found;
					}
				}
			}
		}
	}
	return -EINVAL;
found:
	if (wm8960->clk_id == WM8960_SYSCLK_AUTO) {
		ret = use_pll ? wm8960_set_pll(codec, input, output) :
			wm8960_set_pll(codec, 0, 0);
		if (ret)
			return ret;
	}
	ret = dreem_wm8960_update_bits(codec, WM8960_CLOCK1, 0x1fe,
				 (i << 1) | (j << 3) | (j << 6));
	if (ret < 0)
		return ret;
	ret = dreem_wm8960_update_bits(codec, WM8960_CLOCK2, 0x1cf, (dclk << 6) | k);
	return ret < 0 ? ret : 0;
}

static int wm8960_set_bclk_ratio(struct snd_soc_dai *dai, unsigned int ratio)
{
	struct wm8960_priv *wm8960 = snd_soc_codec_get_drvdata(dai->codec);

	if (ratio != 0 && ratio != 32 && ratio != 64)
		return -EINVAL;
	if ((wm8960->is_stream_in_use[0] || wm8960->is_stream_in_use[1]) &&
	    wm8960->bclk_ratio != ratio)
		return -EBUSY;
	wm8960->bclk_ratio = ratio;
	return 0;
}

static int wm8960_hw_params(struct snd_pcm_substream *substream,
			    struct snd_pcm_hw_params *params,
			    struct snd_soc_dai *dai)
{
	struct snd_soc_codec *codec = dai->codec;
	struct wm8960_priv *wm8960 = snd_soc_codec_get_drvdata(codec);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;
	unsigned int width = params_width(params), rate = params_rate(params);
	unsigned int ratio = wm8960->bclk_ratio ?: width * 2;
	int old_rate = wm8960->lrclk, old_bclk = wm8960->bclk;
	int ret, i, iface, word;

	if (params_channels(params) < 1 || params_channels(params) > 2 ||
	    ratio < width * 2 || ratio > 64)
		return -EINVAL;
	for (i = 0; i < ARRAY_SIZE(alc_rates); i++)
		if (alc_rates[i].rate == rate)
			break;
	if (i == ARRAY_SIZE(alc_rates))
		return -EINVAL;
	switch (width) {
	case 16: word = 0; break;
	case 20: word = 4; break;
	case 24: word = 8; break;
	case 32: word = 12; break;
	default: return -EINVAL;
	}
	if (wm8960->is_stream_in_use[0] || wm8960->is_stream_in_use[1]) {
		if (wm8960->lrclk != rate || wm8960->bclk != rate * ratio ||
		    wm8960->stream_width != width)
			return -EBUSY;
		wm8960->is_stream_in_use[tx] = true;
		return 0;
	}
	iface = snd_soc_read(codec, WM8960_IFACE1);
	if (iface < 0)
		return iface;
	if (width == 32 && !(iface & 3))
		return -EINVAL;
	wm8960->lrclk = rate;
	wm8960->bclk = rate * ratio;
	ret = snd_soc_write(codec, WM8960_IFACE1, (iface & ~12) | word);
	if (ret < 0)
		goto fail;
	ret = wm8960_set_deemph(codec);
	if (ret < 0)
		goto fail;
	ret = dreem_wm8960_update_bits(codec, WM8960_ADDCTL3, 7, alc_rates[i].val);
	if (ret < 0)
		goto fail;
	ret = wm8960_configure_clocking(codec);
	if (ret)
		goto fail;
	wm8960->stream_width = width;
	wm8960->is_stream_in_use[tx] = true;
	return 0;
fail:
	wm8960->lrclk = old_rate;
	wm8960->bclk = old_bclk;
	/* I/O rollback is best effort: retain the original error and never
	 * publish a usable stream, even if the control bus also rejects cleanup.
	 */
	if (wm8960_set_pll(codec, 0, 0))
		dev_warn(codec->dev, "Failed to stop PLL after parameter error\n");
	return ret;
}
