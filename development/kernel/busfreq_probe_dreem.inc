/* SPDX-License-Identifier: GPL-2.0-only */
/* Publish the experimental Femto bus-frequency driver after DDR preparation.
 * NXP initialization APIs/order adapted from busfreq-imx.c, copyright
 * 2011-2016 Freescale Semiconductor and 2017 NXP.
 * New failure handling: copyright 2026 Dreem research contributors. */
static int dreem_busfreq_finish_probe(void)
{
	int ret;

	INIT_DELAYED_WORK(&low_bus_freq_handler, reduce_bus_freq_handler);
	INIT_DELAYED_WORK(&bus_freq_daemon, bus_freq_daemon_handler);
	ret = register_pm_notifier(&imx_bus_freq_pm_notifier);
	if (ret)
		goto discard;
	ret = register_reboot_notifier(&imx_busfreq_reboot_notifier);
	if (ret)
		goto unregister_pm;

	/* Requests use this mutex. On sysfs failure, withdraw readiness before
	 * releasing it, then retire callbacks and queued work before cleanup. */
	mutex_lock(&bus_freq_mutex);
	bus_freq_scaling_is_active = 1;
	bus_freq_scaling_initialized = 1;
	ret = sysfs_create_file(&busfreq_dev->kobj, &dev_attr_enable.attr);
	if (ret) {
		bus_freq_scaling_initialized = 0;
		bus_freq_scaling_is_active = 0;
	}
	mutex_unlock(&bus_freq_mutex);
	if (ret)
		goto unregister_reboot;
	schedule_delayed_work(&bus_freq_daemon, msecs_to_jiffies(10000));
	return 0;

unregister_reboot:
	unregister_reboot_notifier(&imx_busfreq_reboot_notifier);
unregister_pm:
	unregister_pm_notifier(&imx_bus_freq_pm_notifier);
discard:
	cancel_delayed_work_sync(&low_bus_freq_handler);
	cancel_delayed_work_sync(&bus_freq_daemon);
	dreem_ddr_discard_settings();
	return ret;
}
