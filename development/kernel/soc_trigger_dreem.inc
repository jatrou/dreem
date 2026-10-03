/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Checked direct-link trigger sequencing, copyright 2026 Dreem contributors.
 * Adapted from NXP soc-pcm.c (Wolfson/Openedhand/Slimlogic/Texas Instruments).
 * A failing callback may have changed hardware: include it in rollback.
 * Callbacks run in the existing PCM atomic context; never wait for DMA work.
 */
static int dreem_soc_trigger_stage(struct snd_pcm_substream *substream,
				   int cmd, int stage)
{
	struct snd_soc_pcm_runtime *rtd = substream->private_data;
	struct snd_soc_dai *dai;

	if (stage < rtd->num_codecs) {
		dai = rtd->codec_dais[stage];
	} else if (stage == rtd->num_codecs) {
		const struct snd_pcm_ops *ops = rtd->platform->driver->ops;

		return ops && ops->trigger ? ops->trigger(substream, cmd) : 0;
	} else if (stage == rtd->num_codecs + 1) {
		dai = rtd->cpu_dai;
	} else {
		const struct snd_soc_ops *ops = rtd->dai_link->ops;

		return ops && ops->trigger ? ops->trigger(substream, cmd) : 0;
	}
	return dai->driver->ops && dai->driver->ops->trigger ?
		dai->driver->ops->trigger(substream, cmd, dai) : 0;
}

static int dreem_soc_pcm_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct snd_soc_pcm_runtime *rtd = substream->private_data;
	int stage, inverse = -1, ret = 0, error;

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
		inverse = SNDRV_PCM_TRIGGER_STOP;
		break;
	case SNDRV_PCM_TRIGGER_RESUME:
		inverse = SNDRV_PCM_TRIGGER_SUSPEND;
		break;
	case SNDRV_PCM_TRIGGER_PAUSE_RELEASE:
		inverse = SNDRV_PCM_TRIGGER_PAUSE_PUSH;
		break;
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
	case SNDRV_PCM_TRIGGER_PAUSE_PUSH:
		break;
	default:
		return -EINVAL;
	}

	/* Keep this old tree's codec -> platform -> CPU -> machine order. */
	for (stage = 0; stage < rtd->num_codecs + 3; ++stage) {
		error = dreem_soc_trigger_stage(substream, cmd, stage);
		if (error >= 0)
			continue;
		if (!ret)
			ret = error;
		if (inverse >= 0) {
			/* Unwind attempted stages, including the failing stage. */
			for (; stage >= 0; --stage) {
				error = dreem_soc_trigger_stage(substream, inverse, stage);
				if (error < 0)
					dev_err(rtd->dev,
						"trigger rollback stage %d failed: %d (original %d)\n",
						stage, error, ret);
			}
			break;
		}
		/* STOP-like commands attempt every stage despite an earlier error. */
	}
	return ret;
}
