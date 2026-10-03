/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Adapted from fsl_sai.c, copyright 2012-2016 Freescale Semiconductor.
 * Checked configuration and clock ownership by Dreem contributors, 2026.
 * Called from non-atomic DAI setup callbacks, not the PCM trigger path.
 */
static bool dreem_sai_configured(struct fsl_sai *sai)
{
	return sai->dreem_configured[TX] || sai->dreem_configured[RX];
}

static int fsl_sai_set_dai_tdm_slot(struct snd_soc_dai *dai, u32 tx_mask,
		u32 rx_mask, int slots, int width)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(dai);
	int ret = 0;

	if (slots != 2 || width < 16 || width > 32)
		return -EINVAL;
	mutex_lock(&sai->dreem_stream_lock);
	if (dreem_sai_configured(sai) &&
	    (!sai->dreem_explicit_slots || sai->slots != slots || sai->slot_width != width)) {
		ret = -EBUSY;
	} else {
		sai->slots = slots;
		sai->slot_width = width;
		sai->dreem_explicit_slots = true;
	}
	mutex_unlock(&sai->dreem_stream_lock);
	return ret;
}

static int fsl_sai_set_dai_sysclk(struct snd_soc_dai *dai,
		int id, unsigned int freq, int dir)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(dai);
	int ret;

	if (dir == SND_SOC_CLOCK_IN)
		return 0;
	if (dir != SND_SOC_CLOCK_OUT || id < 0 || id >= FSL_SAI_MCLK_MAX)
		return -EINVAL;
	mutex_lock(&sai->dreem_stream_lock);
	if (dreem_sai_configured(sai)) {
		ret = -EBUSY;
		goto out;
	}
	ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_TCR2,
			FSL_SAI_CR2_MSEL_MASK, FSL_SAI_CR2_MSEL(id));
	if (ret < 0)
		goto out;
	ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_RCR2,
			FSL_SAI_CR2_MSEL_MASK, FSL_SAI_CR2_MSEL(id));
out:
	mutex_unlock(&sai->dreem_stream_lock);
	return ret < 0 ? ret : 0;
}

static int fsl_sai_set_dai_fmt(struct snd_soc_dai *dai, unsigned int fmt)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(dai);
	u32 cr2 = 0, cr4 = sai->is_lsb_first ? 0 : FSL_SAI_CR4_MF;
	bool slave = false, dsp = false;
	int tx, ret = 0;

	switch (fmt & SND_SOC_DAIFMT_FORMAT_MASK) {
	case SND_SOC_DAIFMT_I2S:
		cr2 |= FSL_SAI_CR2_BCP;
		cr4 |= FSL_SAI_CR4_FSE | FSL_SAI_CR4_FSP;
		break;
	case SND_SOC_DAIFMT_LEFT_J:
		cr2 |= FSL_SAI_CR2_BCP;
		break;
	case SND_SOC_DAIFMT_DSP_A:
		cr4 |= FSL_SAI_CR4_FSE;
		/* fall through */
	case SND_SOC_DAIFMT_DSP_B:
		cr2 |= FSL_SAI_CR2_BCP;
		dsp = true;
		break;
	default:
		return -EINVAL;
	}
	switch (fmt & SND_SOC_DAIFMT_INV_MASK) {
	case SND_SOC_DAIFMT_IB_IF:
		cr2 ^= FSL_SAI_CR2_BCP;
		cr4 ^= FSL_SAI_CR4_FSP;
		break;
	case SND_SOC_DAIFMT_IB_NF:
		cr2 ^= FSL_SAI_CR2_BCP;
		break;
	case SND_SOC_DAIFMT_NB_IF:
		cr4 ^= FSL_SAI_CR4_FSP;
		break;
	case SND_SOC_DAIFMT_NB_NF:
		break;
	default:
		return -EINVAL;
	}
	switch (fmt & SND_SOC_DAIFMT_MASTER_MASK) {
	case SND_SOC_DAIFMT_CBS_CFS:
		cr2 |= FSL_SAI_CR2_BCD_MSTR;
		cr4 |= FSL_SAI_CR4_FSD_MSTR;
		break;
	case SND_SOC_DAIFMT_CBM_CFM:
		slave = true;
		break;
	case SND_SOC_DAIFMT_CBS_CFM:
		cr2 |= FSL_SAI_CR2_BCD_MSTR;
		break;
	case SND_SOC_DAIFMT_CBM_CFS:
		cr4 |= FSL_SAI_CR4_FSD_MSTR;
		slave = true;
		break;
	default:
		return -EINVAL;
	}
	mutex_lock(&sai->dreem_stream_lock);
	if (dreem_sai_configured(sai)) {
		ret = sai->dreem_format == fmt ? 0 : -EBUSY;
		goto out;
	}
	for (tx = TX; tx >= RX; tx--) {
		ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR2(tx),
			FSL_SAI_CR2_BCP | FSL_SAI_CR2_BCD_MSTR, cr2);
		if (ret < 0)
			goto out;
		ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR4(tx),
			FSL_SAI_CR4_MF | FSL_SAI_CR4_FSE |
			FSL_SAI_CR4_FSP | FSL_SAI_CR4_FSD_MSTR, cr4);
		if (ret < 0)
			goto out;
	}
	/* Never publish a partial format or retain DSP mode from an old format. */
	sai->is_slave_mode = slave;
	sai->is_dsp_mode = dsp;
	sai->dreem_format = fmt;
out:
	mutex_unlock(&sai->dreem_stream_lock);
	return ret < 0 ? ret : 0;
}

static int dreem_sai_clock_plan(struct fsl_sai *sai, u32 freq,
		unsigned int *clock_id, u32 *divider)
{
	u32 best = freq, div = 0, id, selected = 0;
	unsigned long rate, error, ratio;

	if (!freq)
		return -EINVAL;
	for (id = 0; id < FSL_SAI_MCLK_MAX; id++) {
		if (IS_ERR_OR_NULL(sai->mclk_clk[id]))
			continue;
		rate = clk_get_rate(sai->mclk_clk[id]);
		ratio = rate / freq;
		error = rate - ratio * freq;
		if (!rate || (error && rate / error < 1000) ||
		    ratio < 2 || ratio > 512 || ratio % 2)
			continue;
		if (error < best) {
			best = error;
			div = ratio / 2;
			selected = id;
		}
		if (!error)
			break;
	}
	if (!div)
		return -EINVAL;
	*clock_id = selected;
	*divider = div - 1;
	return 0;
}

static int dreem_sai_frame(struct fsl_sai *sai, bool tx,
		u32 cr4, u32 cr5, unsigned int channels)
{
	int ret;

	ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR4(tx),
		FSL_SAI_CR4_SYWD_MASK | FSL_SAI_CR4_FRSZ_MASK, cr4);
	if (ret < 0)
		return ret;
	ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR5(tx),
		FSL_SAI_CR5_WNW_MASK | FSL_SAI_CR5_W0W_MASK | FSL_SAI_CR5_FBT_MASK, cr5);
	if (ret < 0)
		return ret;
	return regmap_write(sai->regmap, FSL_SAI_xMR(tx), ~((1U << channels) - 1));
}

static int fsl_sai_hw_params(struct snd_pcm_substream *substream,
		struct snd_pcm_hw_params *params, struct snd_soc_dai *dai)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(dai);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK, source_tx = tx;
	unsigned int channels = params_channels(params), width = params_width(params);
	u32 rate = params_rate(params), slot, cr4, cr5, div = 0;
	unsigned int id = 0;
	struct clk *owned = NULL;
	int ret = 0;

	mutex_lock(&sai->dreem_stream_lock);
	if (!sai->is_stream_opened[tx]) {
		ret = -EBADFD;
		goto out;
	}
	slot = (sai->dreem_explicit_slots || !sai->is_slave_mode) ? sai->slot_width : width;
	if (!rate || rate > 192000 || channels < 1 || channels > 2 || sai->slots != 2 ||
	    (width != 16 && width != 20 && width != 24 && width != 32) ||
	    slot < width || slot > 32) {
		ret = -EINVAL;
		goto out;
	}
	if (sai->dreem_configured[tx]) {
		ret = sai->dreem_rate[tx] == rate && sai->dreem_width[tx] == width &&
		      sai->dreem_channels[tx] == channels ? 0 : -EBUSY;
		goto out;
	}
	if (sai->synchronous[TX] || sai->synchronous[RX]) {
		if (sai->dreem_configured[!tx] &&
		    (sai->dreem_rate[!tx] != rate || sai->dreem_width[!tx] != width)) {
			ret = -EBUSY;
			goto out;
		}
		source_tx = !sai->synchronous[TX];
	}
	if (!sai->is_slave_mode) {
		if (sai->synchronous[TX] && sai->synchronous[RX]) {
			ret = -EINVAL;
			goto out;
		}
		if (sai->dreem_configured[!tx] &&
		    (sai->synchronous[TX] || sai->synchronous[RX])) {
			id = sai->mclk_id[!tx];
		} else {
			ret = dreem_sai_clock_plan(sai, sai->slots * slot * rate, &id, &div);
			if (ret)
				goto out;
			ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR2(source_tx),
				FSL_SAI_CR2_MSEL_MASK | FSL_SAI_CR2_DIV_MASK,
				FSL_SAI_CR2_MSEL(id) | div);
			if (ret < 0)
				goto out;
		}
		ret = clk_prepare_enable(sai->mclk_clk[id]);
		if (ret)
			goto out;
		owned = sai->mclk_clk[id];
	}
	cr4 = FSL_SAI_CR4_FRSZ(sai->slots);
	if (!sai->is_dsp_mode)
		cr4 |= FSL_SAI_CR4_SYWD(slot);
	cr5 = FSL_SAI_CR5_WNW(slot) | FSL_SAI_CR5_W0W(slot) |
		FSL_SAI_CR5_FBT(sai->is_lsb_first ? 0 : width - 1);
	if (!sai->is_slave_mode && source_tx != tx && !sai->dreem_configured[source_tx]) {
		ret = dreem_sai_frame(sai, source_tx, cr4, cr5, channels);
		if (ret < 0)
			goto release;
	}
	ret = dreem_sai_frame(sai, tx, cr4, cr5, channels);
	if (ret < 0)
		goto release;
	sai->dreem_owned_mclk[tx] = owned;
	if (owned) {
		sai->mclk_id[tx] = id;
		sai->mclk_streams |= BIT(substream->stream);
	}
	sai->dreem_rate[tx] = rate;
	sai->dreem_width[tx] = width;
	sai->dreem_channels[tx] = channels;
	sai->dreem_configured[tx] = true;
	ret = 0;
	goto out;
release:
	/* ASoC skips our hw_free when this callback fails. */
	if (owned)
		clk_disable_unprepare(owned);
out:
	mutex_unlock(&sai->dreem_stream_lock);
	return ret;
}

static void dreem_sai_release_params(struct fsl_sai *sai, bool tx)
{
	if (sai->dreem_owned_mclk[tx]) {
		clk_disable_unprepare(sai->dreem_owned_mclk[tx]);
		sai->dreem_owned_mclk[tx] = NULL;
	}
	sai->mclk_streams &= ~BIT(tx ? SNDRV_PCM_STREAM_PLAYBACK : SNDRV_PCM_STREAM_CAPTURE);
	sai->dreem_configured[tx] = false;
	sai->dreem_rate[tx] = sai->dreem_width[tx] = sai->dreem_channels[tx] = 0;
}

static int fsl_sai_hw_free(struct snd_pcm_substream *substream, struct snd_soc_dai *dai)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(dai);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;

	mutex_lock(&sai->dreem_stream_lock);
	dreem_sai_release_params(sai, tx);
	mutex_unlock(&sai->dreem_stream_lock);
	return 0;
}
