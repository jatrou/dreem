/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Stream handling adapted from NXP imx-wm8960.c, copyright 2015-2016
 * Freescale Semiconductor. State/clock ownership fixes by Dreem contributors,
 * copyright 2026. Codec-master mode uses two explicit 32-bit serial slots.
 */
static int imx_hifi_hw_params(struct snd_pcm_substream *substream,
			    struct snd_pcm_hw_params *params)
{
	struct snd_soc_pcm_runtime *rtd = substream->private_data;
	struct snd_soc_dai *codec_dai = rtd->codec_dai;
	struct snd_soc_dai *cpu_dai = rtd->cpu_dai;
	struct imx_wm8960_data *data = snd_soc_card_get_drvdata(rtd->card);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;
	unsigned int rate = params_rate(params), width = params_width(params);
	unsigned int fmt;
	int ret;

	if (!data->is_stream_opened[tx])
		return -EBADFD;
	if (rate < 8000 || rate > 48000 ||
	    (width != 16 && width != 20 && width != 24 && width != 32))
		return -EINVAL;
	/* The two directions share the clock and serial word-length setting. */
	if (data->is_stream_in_use[!tx]) {
		if (rate != data->stream_rate[!tx] || width != data->stream_width[!tx])
			return -EINVAL;
		goto configured;
	}
	data->is_stream_in_use[tx] = false;
	fmt = SND_SOC_DAIFMT_I2S | SND_SOC_DAIFMT_NB_NF |
		(data->is_codec_master ? SND_SOC_DAIFMT_CBM_CFM : SND_SOC_DAIFMT_CBS_CFS);
	ret = snd_soc_dai_set_fmt(cpu_dai, fmt);
	if (ret)
		return ret;
	ret = snd_soc_dai_set_fmt(codec_dai, fmt);
	if (ret)
		return ret;
	ret = snd_soc_dai_set_bclk_ratio(codec_dai, data->is_codec_master ? 64 : 0);
	if (ret)
		return ret;
	if (!data->is_codec_master) {
		ret = snd_soc_dai_set_tdm_slot(cpu_dai, 0, 0, 2, width);
		if (ret)
			return ret;
		ret = snd_soc_dai_set_sysclk(cpu_dai, 0, 0, SND_SOC_CLOCK_OUT);
		if (ret)
			return ret;
	} else {
		ret = snd_soc_dai_set_tdm_slot(cpu_dai, 0, 0, 2, 32);
		if (ret)
			return ret;
		ret = snd_soc_dai_set_sysclk(cpu_dai, 0, 0, SND_SOC_CLOCK_IN);
		if (ret)
			return ret;
		data->clk_frequency = clk_get_rate(data->codec_clk);
		if (!data->clk_frequency)
			return -EINVAL;
		ret = snd_soc_dai_set_pll(codec_dai, WM8960_SYSCLK_AUTO, 0,
					data->clk_frequency, 0);
		if (ret)
			return ret;
		ret = snd_soc_dai_set_sysclk(codec_dai, WM8960_SYSCLK_AUTO, 0, 0);
		if (ret)
			return ret;
	}
configured:
	data->stream_rate[tx] = rate;
	data->stream_width[tx] = width;
	data->is_stream_in_use[tx] = true;
	return 0;
}

static int imx_hifi_hw_free(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = substream->private_data;
	struct imx_wm8960_data *data = snd_soc_card_get_drvdata(rtd->card);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;

	data->is_stream_in_use[tx] = false;
	data->stream_rate[tx] = 0;
	data->stream_width[tx] = 0;
	if (data->is_codec_master && !data->is_stream_in_use[!tx])
		return snd_soc_dai_set_fmt(rtd->codec_dai,
			SND_SOC_DAIFMT_CBS_CFS | SND_SOC_DAIFMT_I2S | SND_SOC_DAIFMT_NB_NF);
	return 0;
}

static u32 imx_wm8960_rates[] = { 8000, 16000, 32000, 48000 };
static struct snd_pcm_hw_constraint_list imx_wm8960_rate_constraints = {
	.count = ARRAY_SIZE(imx_wm8960_rates),
	.list = imx_wm8960_rates,
};

static int imx_hifi_startup(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = substream->private_data;
	struct imx_wm8960_data *data = snd_soc_card_get_drvdata(rtd->card);
	struct fsl_sai *sai = dev_get_drvdata(rtd->cpu_dai->dev);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;
	int ret;

	/* CPU startup runs first. Do not publish ownership until MCLK succeeds. */
	if (data->is_stream_opened[tx] || !sai->is_stream_opened[tx] ||
	    data->is_stream_opened[!tx] != sai->is_stream_opened[!tx])
		return -EBUSY;
	if (!data->is_codec_master) {
		ret = snd_pcm_hw_constraint_list(substream->runtime, 0,
				SNDRV_PCM_HW_PARAM_RATE, &imx_wm8960_rate_constraints);
		if (ret)
			return ret;
	}
	ret = clk_prepare_enable(data->codec_clk);
	if (ret)
		return ret;
	data->is_stream_opened[tx] = true;
	return 0;
}

static void imx_hifi_shutdown(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = substream->private_data;
	struct imx_wm8960_data *data = snd_soc_card_get_drvdata(rtd->card);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;

	if (data->is_stream_opened[tx]) {
		clk_disable_unprepare(data->codec_clk);
		data->is_stream_opened[tx] = false;
	}
	data->is_stream_in_use[tx] = false;
	data->stream_rate[tx] = 0;
	data->stream_width[tx] = 0;
}
