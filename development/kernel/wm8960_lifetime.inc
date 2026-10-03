/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Femto board setup based on NXP imx-wm8960.c (2015-2016 Freescale).
 * New resource ownership and cleanup: copyright 2026 Dreem contributors.
 * Included after the public board's helpers and DAI definitions.
 */
static void dreem_audio_cleanup(struct platform_device *pdev,
				struct imx_wm8960_data *data)
{
	/* Wait for any ioctl to finish before withdrawing its sound pointers. */
	mutex_lock(&dreem_jack_mutex);
	dreem_jack_ready = false;
	mutex_unlock(&dreem_jack_mutex);
	if (data) {
		/* misc_open takes misc_mtx before our mutex: never invert that order. */
		if (data->jack_registered)
			misc_deregister(&dreem_jack_device);
		if (data->mic_attribute)
			driver_remove_file(pdev->dev.driver, &driver_attr_micphone);
		if (data->hp_attribute)
			driver_remove_file(pdev->dev.driver, &driver_attr_headphone);
		/* These calls drain IRQs/work while the card and controls still live. */
		if (data->mic_gpio_added)
			snd_soc_jack_free_gpios(&imx_mic_jack, 1, &imx_mic_jack_gpio);
		if (data->hp_gpio_added)
			snd_soc_jack_free_gpios(&imx_hp_jack, 1, &imx_hp_jack_gpio);
		if (data->card_registered)
			snd_soc_unregister_card(&data->card);
		if (!IS_ERR_OR_NULL(data->codec_clk))
			clk_put(data->codec_clk);
		if (data->gpr_changed && regmap_update_bits(data->gpr,
				data->gpr_register, data->gpr_mask, data->gpr_before))
			dev_warn(&pdev->dev, "could not restore audio GPR field\n");
		of_node_put(data->cpu_node);
		of_node_put(data->codec_node);
		of_node_put(data->asrc_node);
		if (data->cpu_device)
			put_device(&data->cpu_device->dev);
		if (data->codec_device)
			put_device(&data->codec_device->dev);
		if (data->asrc_device)
			put_device(&data->asrc_device->dev);
	}
	platform_set_drvdata(pdev, NULL);
	/* Keep ownership until all external callbacks and sound users drained. */
	mutex_lock(&dreem_jack_mutex);
	memset(&card_priv, 0, sizeof(card_priv));
	memset(&imx_hp_jack, 0, sizeof(imx_hp_jack));
	memset(&imx_mic_jack, 0, sizeof(imx_mic_jack));
	dreem_jack_device.parent = NULL;
	kfree(data);
	dreem_audio_owner = NULL;
	mutex_unlock(&dreem_jack_mutex);
}

static int imx_wm8960_probe(struct platform_device *pdev)
{
	struct imx_wm8960_data *data = NULL;
	struct imx_priv *priv = &card_priv;
	struct device_node *np = pdev->dev.of_node;
	struct of_phandle_args args;
	u32 hardware, width;
	int ret;

	ret = dreem_get_hardware_version(&hardware);
	if (ret)
		return ret;
	/* The matched WM8960 path belongs to the nonzero hardware branch. */
	if (!hardware)
		return -ENODEV;
	mutex_lock(&dreem_jack_mutex);
	if (dreem_audio_owner || dreem_jack_generation == ULONG_MAX) {
		ret = dreem_audio_owner ? -EBUSY : -EOVERFLOW;
		mutex_unlock(&dreem_jack_mutex);
		return ret;
	}
	dreem_audio_owner = pdev;
	++dreem_jack_generation;
	mutex_unlock(&dreem_jack_mutex);

	data = kzalloc(sizeof(*data), GFP_KERNEL);
	if (!data) {
		ret = -ENOMEM;
		goto fail;
	}
	priv->pdev = pdev;
	data->cpu_node = of_parse_phandle(np, "cpu-dai", 0);
	data->codec_node = of_parse_phandle(np, "audio-codec", 0);
	if (!data->cpu_node || !data->codec_node) {
		ret = -EINVAL;
		goto fail;
	}
	data->cpu_device = of_find_device_by_node(data->cpu_node);
	data->codec_device = of_find_i2c_device_by_node(data->codec_node);
	if (!data->cpu_device || !data->codec_device || !data->codec_device->dev.driver) {
		ret = -EPROBE_DEFER;
		goto fail;
	}
	data->is_codec_master = of_property_read_bool(np, "codec-master");
	/* A non-devm reference: teardown belongs to this machine, not the codec. */
	data->codec_clk = clk_get(&data->codec_device->dev, "mclk");
	if (IS_ERR(data->codec_clk)) {
		ret = PTR_ERR(data->codec_clk);
		goto fail;
	}
	ret = of_parse_phandle_with_fixed_args(np, "gpr", 3, 0, &args);
	if (ret)
		goto fail;
	data->gpr = syscon_node_to_regmap(args.np);
	of_node_put(args.np);
	if (IS_ERR(data->gpr)) {
		ret = PTR_ERR(data->gpr);
		goto fail;
	}
	data->gpr_register = args.args[0];
	data->gpr_mask = args.args[1];
	ret = regmap_read(data->gpr, data->gpr_register, &data->gpr_before);
	if (ret)
		goto fail;
	data->gpr_changed = true;
	ret = regmap_update_bits(data->gpr, data->gpr_register,
				 data->gpr_mask, args.args[2]);
	if (ret)
		goto fail;
	of_property_read_u32_array(np, "hp-det", data->hp_det, 2);
	data->asrc_node = of_parse_phandle(np, "asrc-controller", 0);
	if (data->asrc_node) {
		data->asrc_device = of_find_device_by_node(data->asrc_node);
		if (!data->asrc_device) {
			ret = -EPROBE_DEFER;
			goto fail;
		}
	}
	priv->asrc_pdev = data->asrc_device;
	/* ASoC mutates its links; each binding starts from the original template. */
	memcpy(data->links, imx_wm8960_dai, sizeof(data->links));
	data->card.dai_link = data->links;
	data->links[0].codec_of_node = data->codec_node;
	data->links[0].cpu_dai_name = dev_name(&data->cpu_device->dev);
	data->links[0].platform_of_node = data->cpu_node;
	data->card.num_links = 1;
	if (data->asrc_device) {
		data->links[1].cpu_of_node = data->asrc_node;
		data->links[1].platform_of_node = data->asrc_node;
		data->links[2].codec_of_node = data->codec_node;
		data->links[2].cpu_dai_name = dev_name(&data->cpu_device->dev);
		data->card.num_links = 3;
		ret = of_property_read_u32(data->asrc_node, "fsl,asrc-rate", &data->asrc_rate);
		if (ret)
			goto fail;
		ret = of_property_read_u32(data->asrc_node, "fsl,asrc-width", &width);
		if (ret)
			goto fail;
		if (width != 16 && width != 24) {
			ret = -EINVAL;
			goto fail;
		}
		data->asrc_format = width == 24 ? SNDRV_PCM_FORMAT_S24_LE : SNDRV_PCM_FORMAT_S16_LE;
	}
	data->card.dev = &pdev->dev;
	data->card.owner = THIS_MODULE;
	ret = snd_soc_of_parse_card_name(&data->card, "model");
	if (ret)
		goto fail;
	data->card.dapm_widgets = imx_wm8960_dapm_widgets;
	data->card.num_dapm_widgets = ARRAY_SIZE(imx_wm8960_dapm_widgets);
	ret = snd_soc_of_parse_audio_routing(&data->card, "audio-routing");
	if (ret)
		goto fail;
	data->card.late_probe = imx_wm8960_late_probe;
	snd_soc_card_set_drvdata(&data->card, data);
	ret = snd_soc_register_card(&data->card);
	if (ret)
		goto fail;
	data->card_registered = true;
	priv->snd_card = data->card.snd_card;

	imx_hp_jack_gpio.gpio = of_get_named_gpio_flags(np, "hp-det-gpios", 0, &priv->hp_active_low);
	if (!gpio_is_valid(imx_hp_jack_gpio.gpio)) {
		ret = imx_hp_jack_gpio.gpio < 0 ? imx_hp_jack_gpio.gpio : -EINVAL;
		goto fail;
	}
	priv->hp_active_low &= OF_GPIO_ACTIVE_LOW;
	imx_mic_jack_gpio.gpio = of_get_named_gpio_flags(np, "mic-det-gpios", 0, &priv->mic_active_low);
	if (imx_mic_jack_gpio.gpio != -ENOENT && !gpio_is_valid(imx_mic_jack_gpio.gpio)) {
		ret = imx_mic_jack_gpio.gpio < 0 ? imx_mic_jack_gpio.gpio : -EINVAL;
		goto fail;
	}
	priv->mic_active_low &= OF_GPIO_ACTIVE_LOW;
	priv->is_headset_jack = imx_hp_jack_gpio.gpio == imx_mic_jack_gpio.gpio;
	priv->headphone_kctl = snd_kctl_jack_new("Headphone", 0, NULL);
	if (!priv->headphone_kctl) {
		ret = -ENOMEM;
		goto fail;
	}
	ret = snd_ctl_add(priv->snd_card, priv->headphone_kctl);
	if (ret) {
		/* snd_ctl_add frees its argument on failure. */
		priv->headphone_kctl = NULL;
		goto fail;
	}
	imx_hp_jack_pin.mask = SND_JACK_HEADPHONE;
	imx_hp_jack_gpio.report = SND_JACK_HEADPHONE;
	if (priv->is_headset_jack) {
		imx_hp_jack_pin.mask |= SND_JACK_MICROPHONE;
		imx_hp_jack_gpio.report |= SND_JACK_MICROPHONE;
	}
	imx_hp_jack_gpio.jack_status_check = hp_jack_status_check;
	imx_hp_jack_gpio.data = &imx_hp_jack;
	ret = imx_wm8960_jack_init(&data->card, &imx_hp_jack, &imx_hp_jack_pin, &imx_hp_jack_gpio);
	if (ret)
		goto fail;
	data->hp_gpio_added = true;
	ret = driver_create_file(pdev->dev.driver, &driver_attr_headphone);
	if (ret)
		goto fail;
	data->hp_attribute = true;
	if (gpio_is_valid(imx_mic_jack_gpio.gpio)) {
		if (!priv->is_headset_jack) {
			imx_mic_jack_gpio.jack_status_check = mic_jack_status_check;
			imx_mic_jack_gpio.data = &imx_mic_jack;
			ret = imx_wm8960_jack_init(&data->card, &imx_mic_jack,
					&imx_mic_jack_pins, &imx_mic_jack_gpio);
			if (ret)
				goto fail;
			data->mic_gpio_added = true;
		}
		ret = driver_create_file(pdev->dev.driver, &driver_attr_micphone);
		if (ret)
			goto fail;
		data->mic_attribute = true;
	}
	dreem_jack_device.minor = MISC_DYNAMIC_MINOR;
	dreem_jack_device.parent = &pdev->dev;
	ret = misc_register(&dreem_jack_device);
	if (ret)
		goto fail;
	data->jack_registered = true;
	platform_set_drvdata(pdev, data);
	mutex_lock(&dreem_jack_mutex);
	dreem_jack_ready = true;
	mutex_unlock(&dreem_jack_mutex);
	return 0;
fail:
	dreem_audio_cleanup(pdev, data);
	return ret;
}

static int imx_wm8960_remove(struct platform_device *pdev)
{
	/* Driver core serializes probe/remove on a platform device. */
	mutex_lock(&dreem_jack_mutex);
	if (dreem_audio_owner != pdev) {
		mutex_unlock(&dreem_jack_mutex);
		return -ENODEV;
	}
	mutex_unlock(&dreem_jack_mutex);
	dreem_audio_cleanup(pdev, platform_get_drvdata(pdev));
	return 0;
}
