/* SPDX-License-Identifier: GPL-2.0-only */
/* Private imx-sdma extension state. Included only by the research overlay. */
#include <linux/mutex.h>
#include "sdma_eeg.h"

struct dreem_sdma {
	struct mutex lock;
	spinlock_t progress_lock;
	struct sdma_eeg_progress progress;
	u32 registers[8], pc;
	u8 *ring;
	u32 *counter;
	dma_addr_t ring_phys, counter_phys;
	int irq, error;
	bool enabled, firmware_done, script_loaded, armed, published, removed;
	bool clocks_held, command_failed;
};

static bool dreem_eeg_enabled;
static bool dreem_ram_tail_confirmed;
module_param(dreem_eeg_enabled, bool, 0400);
module_param(dreem_ram_tail_confirmed, bool, 0400);
MODULE_PARM_DESC(dreem_eeg_enabled,
	"Enable the experimental Femto channel-1 provider; prevents system sleep");
MODULE_PARM_DESC(dreem_ram_tail_confirmed,
	"Allow EEG placement after external firmware only after its RAM use is verified");

static void dreem_sdma_irq(struct sdma_engine *sdma);
static void dreem_sdma_command_failed(struct sdma_engine *sdma);
