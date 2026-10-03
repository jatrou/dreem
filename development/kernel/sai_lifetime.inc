/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Adapted from fsl_sai.c, copyright 2012-2016 Freescale Semiconductor.
 * Stream acquisition/rollback by Dreem research contributors, 2026.
 */
/* Caller holds the stream mutex and has confirmed this stream is stopped. */
static void dreem_sai_close_resources(struct fsl_sai *sai,
		struct snd_soc_dai *dai, bool tx)
{
	dreem_sai_release_params(sai, tx);
	if (dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR3(tx),
				    FSL_SAI_CR3_TRCE, 0) < 0)
		dev_err(dai->dev, "SAI shutdown channel cleanup failed\n");
	clk_disable_unprepare(sai->bus_clk);
	sai->is_stream_opened[tx] = false;
	sai->dreem_orphaned[tx] = false;
	pm_runtime_put_sync(dai->dev);
}

static int fsl_sai_startup(struct snd_pcm_substream *substream,
		struct snd_soc_dai *cpu_dai)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(cpu_dai);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;
	int ret;

	/* Direct and ASRC links can have different ASoC runtime mutexes. */
	mutex_lock(&sai->dreem_stream_lock);
	/* A failed void shutdown retained clocks; retry its stop before reusing. */
	if (sai->is_stream_opened[tx] && sai->dreem_orphaned[tx]) {
		ret = dreem_sai_stop_stream(sai, tx);
		if (ret)
			goto unlock;
		dreem_sai_close_resources(sai, cpu_dai, tx);
	}
	if (sai->is_stream_opened[tx]) {
		ret = -EBUSY;
		goto unlock;
	}
	ret = pm_runtime_get_sync(cpu_dai->dev);
	if (ret < 0) {
		/* get_sync increments usage even when resume fails. */
		pm_runtime_put_noidle(cpu_dai->dev);
		goto unlock;
	}
	ret = clk_prepare_enable(sai->bus_clk);
	if (ret)
		goto put_pm;
	ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR3(tx),
				     FSL_SAI_CR3_TRCE, FSL_SAI_CR3_TRCE);
	if (ret < 0)
		goto clear_channel;
	ret = snd_pcm_hw_constraint_list(substream->runtime, 0,
			SNDRV_PCM_HW_PARAM_RATE, &fsl_sai_rate_constraints);
	if (ret < 0)
		goto clear_channel;
	/* ASoC will not call shutdown if this callback itself fails. */
	sai->is_stream_opened[tx] = true;
	ret = 0;
	goto unlock;

clear_channel:
	if (dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR3(tx),
				    FSL_SAI_CR3_TRCE, 0) < 0)
		dev_err(cpu_dai->dev, "SAI startup channel cleanup failed\n");
	clk_disable_unprepare(sai->bus_clk);
put_pm:
	pm_runtime_put_sync(cpu_dai->dev);
unlock:
	mutex_unlock(&sai->dreem_stream_lock);
	return ret;
}

static void fsl_sai_shutdown(struct snd_pcm_substream *substream,
		struct snd_soc_dai *cpu_dai)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(cpu_dai);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;

	mutex_lock(&sai->dreem_stream_lock);
	if (sai->is_stream_opened[tx]) {
		if (dreem_sai_stop_stream(sai, tx)) {
			sai->dreem_orphaned[tx] = true;
			dev_err(cpu_dai->dev, "SAI shutdown retained clocks after stop failure\n");
		} else {
			dreem_sai_close_resources(sai, cpu_dai, tx);
		}
	}
	mutex_unlock(&sai->dreem_stream_lock);
}
