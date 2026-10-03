/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DREEM_SDMA_EEG_API_H
#define DREEM_SDMA_EEG_API_H

#define DREEM_SDMA_CLAIM   0
#define DREEM_SDMA_PAUSE   1
#define DREEM_SDMA_RUN     2
#define DREEM_SDMA_RELEASE 3

/* Sleepable, serialized single-consumer interface. CLAIM requires the new
 * managed script and an acknowledged pause. PAUSE returning zero establishes
 * DMA quiescence even if a previous error remains latched in status(). A
 * failed PAUSE never authorizes SPI access or release of clocks/storage.
 * RELEASE relinquishes the claim only after a successful PAUSE. */
#ifdef CONFIG_DREEM_EEG_SDMA
int dreem_sdma_status(void);
int dreem_sdma_control(unsigned int command);
#else
/* Stock compatibility build has no cooperative-stop proof. */
static inline int dreem_sdma_status(void) { return 0; }
static inline int dreem_sdma_control(unsigned int command) { return 0; }
#endif
#endif
