import sys

from migen.fhdl.std import *
from migen.genlib.fsm import FSM, NextState, NextValue

from migen.fhdl import verilog

import riffa, replacementpolicies, pagetransfer

class GenericRiffa(Module):
	def __init__(self, combined_interface_rx, combined_interface_tx, c_pci_data_width=32, drive_clocks=True):
		self.combined_interface_tx = combined_interface_tx
		self.combined_interface_rx = combined_interface_rx
		self.c_pci_data_width = c_pci_data_width
		self.submodules.channelsplitter = riffa.ChannelSplitter(combined_interface_rx, combined_interface_tx)

		num_chnls = flen(combined_interface_rx.start)
		if drive_clocks:
			self.rx_clk = Signal(num_chnls)
			self.tx_clk = Signal(num_chnls)
			self.comb += [ self.rx_clk[i].eq(ClockSignal()) for i in range(num_chnls) ]
			self.comb += [ self.tx_clk[i].eq(ClockSignal()) for i in range(num_chnls) ]


	def get_channel(self, i):
		return self.channelsplitter.get_channel(i)



class Virtmem(Module):

	def __init__(self, rx0, tx0, rx1, tx1, c_pci_data_width=32, wordsize=32, ptrsize=64, npagesincache=4, pagesize=4096):
		self.cmd_rx = rx0
		self.cmd_tx = tx0
		self.data_rx = rx1
		self.data_tx = tx1
		self.req = Signal()
		self.virt_addr = Signal(ptrsize)
		self.num_words = Signal(ptrsize)
		self.data_read = Signal(wordsize)
		self.data_valid = Signal()
		self.done = Signal()
		self.data_write = Signal(wordsize)
		self.write_enable = Signal()
		self.write_ack = Signal()
		self.flush_all = Signal()
		###

		#register I/Os
		virt_addr_p = Signal(ptrsize)
		req_p = Signal()
		num_words_p = Signal(ptrsize)
		data_write_p = Signal(wordsize)
		write_enable_p = Signal()
		flush_all_p = Signal()

		self.sync += virt_addr_p.eq(self.virt_addr), req_p.eq(self.req), data_write_p.eq(self.data_write), write_enable_p.eq(self.write_enable), flush_all_p.eq(self.flush_all), num_words_p.eq(self.num_words)

		self.data_valid_n = Signal()
		self.sync += self.data_valid.eq(self.data_valid_n)

		self.virt_addr_internal = Signal(ptrsize)

		#constant definitions
		memorywidth = max(c_pci_data_width, wordsize)
		memorysize = npagesincache*pagesize*8//memorywidth

		pcie_word_adr_nbits = log2_int(memorywidth//32)
		num_tx_off = log2_int(c_pci_data_width//32)

		num_tx_per_word = max(1, wordsize//c_pci_data_width)

		words_per_line = c_pci_data_width//wordsize if c_pci_data_width > wordsize else wordsize//c_pci_data_width

		page_adr_nbits = log2_int(npagesincache)
		line_adr_nbits = log2_int(pagesize*8//memorywidth)
		word_adr_nbits = log2_int(words_per_line)
		byte_adr_nbits = log2_int(wordsize//8)

		word_adr_off = byte_adr_nbits
		line_adr_off = log2_int(memorywidth//8)
		page_tag_off = line_adr_nbits + line_adr_off

		page_tag_nbits = ptrsize - page_tag_off


		# cache memory
		self.specials.mem = Memory(memorywidth, memorysize, init=[i+0xABBA for i in range(memorysize)])
		
		self.specials.rd_port = rd_port = self.mem.get_port(has_re=True)

		self.specials.wr_port = wr_port = self.mem.get_port(write_capable=True, we_granularity=min(wordsize, c_pci_data_width))
		

		# cache status
		pg_adr = Signal(page_adr_nbits)

		page_tags = Array(Signal(page_tag_nbits, name="page_tags") for i in range(npagesincache))
		page_valid = Array(Signal(name="page_valid") for i in range(npagesincache))
		page_dirty = Array(Signal(name="page_dirty") for i in range(npagesincache))

		found = Signal()
		cache_hit_en = Signal()

		self.comb += [If((page_tags[i] == self.virt_addr_internal[page_tag_off:ptrsize]) & page_valid[i], found.eq(1), pg_adr.eq(i)) for i in range(npagesincache)]
		
		# replacement policy
		self.submodules.replacement_policy = replacementpolicies.TrueLRU(npages=npagesincache)
		pg_to_replace = self.replacement_policy.pg_to_replace
		self.comb += self.replacement_policy.hit.eq(found & cache_hit_en), self.replacement_policy.pg_adr.eq(pg_adr)

		# page transfer module
		self.submodules.pagetransferrer = pagetransfer.PageTransferrer(rx0, tx0, rx1, tx1, rd_port, wr_port, c_pci_data_width=c_pci_data_width, wordsize=wordsize, ptrsize=ptrsize, npagesincache=npagesincache, pagesize=pagesize)

		# state machine that controls page cache
		page_control_fsm = FSM()
		self.submodules += page_control_fsm

		# internal FSM signals

		flush_initiated = Signal()
		flush_done = Signal()
		pg_to_flush = Signal(page_adr_nbits)

		pg_to_writeback = Signal(page_adr_nbits)

		self.comb += pg_to_writeback.eq(Mux(flush_initiated, pg_to_flush, pg_to_replace))

		num_retransmissions = Signal(8)
		max_retransmissions = 1

		word_select = Signal(word_adr_nbits)
		burst_end_addr = Signal(ptrsize)
		virt_addr_reg = Signal(ptrsize)
		next_virt_addr = Signal(ptrsize)
		use_input_virt_addr = Signal()
		self.comb += If(use_input_virt_addr, self.virt_addr_internal.eq(virt_addr_p)).Else(self.virt_addr_internal.eq(virt_addr_reg))

		last_word = Signal()
		crossed_page_boundary = Signal()

		page_control_fsm.act("IDLE", 
			#reset internal registers
			NextValue(num_retransmissions, 0),
			use_input_virt_addr.eq(1),
			# react to inputs
			If(req_p,
				NextValue(virt_addr_reg, virt_addr_p),
				NextValue(next_virt_addr, virt_addr_p + (1 << byte_adr_nbits)),
				NextValue(burst_end_addr, virt_addr_p + (num_words_p << byte_adr_nbits)),
				If(found,
					If(write_enable_p,
						NextState("WRITE_DATA")
					).Else(
						NextState("GET_DATA")
					)
				).Else(
					If(page_dirty[pg_to_replace],
						NextState("PAGE_WB_INIT")
					).Else(
						NextState("PAGE_FETCH_INIT")
					)
				)
			).Elif(flush_all_p,
				NextState("FLUSH_DIRTY")
			).Elif(self.cmd_rx.start,
				NextState("RX_CMD")
			)
		)

		page_control_fsm.act("GET_DATA", 
			cache_hit_en.eq(1),
			rd_port.adr.eq(Cat(self.virt_addr_internal[line_adr_off:line_adr_off + line_adr_nbits], pg_adr)),
			rd_port.re.eq(1),
			NextValue(word_select, self.virt_addr_internal[word_adr_off:word_adr_off+word_adr_nbits]),
			self.data_valid_n.eq(1),
			NextValue(virt_addr_reg, virt_addr_reg + (1 << byte_adr_nbits)),
			NextValue(next_virt_addr, next_virt_addr + (1 << byte_adr_nbits)),
			NextValue(last_word, next_virt_addr >= burst_end_addr),
			NextValue(crossed_page_boundary, virt_addr_reg[page_tag_off:] != next_virt_addr[page_tag_off:]),
			NextState("SERVE_DATA")
		)
		page_control_fsm.act("SERVE_DATA", 
			[If(word_select == i, self.data_read.eq(rd_port.dat_r[i*wordsize:(i+1)*wordsize])) for i in range(c_pci_data_width//wordsize)]
			if c_pci_data_width > wordsize else
			self.data_read.eq(rd_port.dat_r),
			If(~last_word,
				If(~crossed_page_boundary,
					cache_hit_en.eq(1),
					rd_port.adr.eq(Cat(self.virt_addr_internal[line_adr_off:line_adr_off + line_adr_nbits], pg_adr)),
					rd_port.re.eq(1),
					NextValue(word_select, self.virt_addr_internal[word_adr_off:word_adr_off+word_adr_nbits]),
					NextValue(virt_addr_reg, virt_addr_reg + (1 << byte_adr_nbits)),
					NextValue(next_virt_addr, next_virt_addr + (1 << byte_adr_nbits)),
					NextValue(last_word, next_virt_addr >= burst_end_addr),
					NextValue(crossed_page_boundary, virt_addr_reg[page_tag_off:] != next_virt_addr[page_tag_off:]),
					self.data_valid_n.eq(1),
				).Else(
					If(found,
						NextState("GET_DATA")
					).Else(
						If(page_dirty[pg_to_replace],
							NextState("PAGE_WB_INIT")
						).Else(
							NextState("PAGE_FETCH_INIT")
						)
					)
				)
			).Else(
				NextState("DONE")
			)
		)
		page_control_fsm.act("WRITE_DATA", 
			If(found,
				cache_hit_en.eq(1),
				wr_port.dat_w.eq(Cat([data_write_p for i in range(words_per_line)]))
				if c_pci_data_width > wordsize else
				wr_port.dat_w.eq(data_write_p),
				wr_port.we.eq(1 << self.virt_addr_internal[word_adr_off:word_adr_off+word_adr_nbits])
				if c_pci_data_width > wordsize else
				[wr_port.we[i].eq(1) for i in range(words_per_line)],
				wr_port.adr.eq(Cat(self.virt_addr_internal[line_adr_off:line_adr_off + line_adr_nbits], pg_adr)),
				NextValue(page_dirty[pg_adr], 1),
				self.write_ack.eq(1),
				If((virt_addr_reg + (1 << byte_adr_nbits)) < burst_end_addr,
					NextValue(virt_addr_reg, virt_addr_reg + (1 << byte_adr_nbits))
				).Else(
					NextState("DONE")
				)
			).Else(
				If(page_dirty[pg_to_replace],
					NextState("PAGE_WB_INIT")
				).Else(
					NextState("PAGE_FETCH_INIT")
				)
			)	
		)

		page_control_fsm.act("PAGE_FETCH_INIT",
			self.pagetransferrer.virt_addr.eq(0),
			self.pagetransferrer.virt_addr[page_tag_off:].eq(self.virt_addr_internal[page_tag_off:]),
			self.pagetransferrer.page_addr.eq(pg_to_replace),
			self.pagetransferrer.fetch_req.eq(1),
			NextState("PAGE_FETCH_WAIT")
		)
		page_control_fsm.act("PAGE_FETCH_WAIT",
			If(self.pagetransferrer.req_complete,
				NextValue(page_tags[pg_to_replace], self.virt_addr_internal[page_tag_off:]),
				NextValue(page_valid[pg_to_replace], 1),
				If(write_enable_p,
					NextState("WRITE_DATA")
				).Else(
					NextState("GET_DATA")
				)
			)
		)

		page_control_fsm.act("PAGE_WB_INIT",
			self.pagetransferrer.virt_addr.eq(0),
			self.pagetransferrer.virt_addr[page_tag_off:].eq(page_tags[pg_to_writeback]),
			self.pagetransferrer.page_addr.eq(pg_to_writeback),
			self.pagetransferrer.send_req.eq(1),
			NextState("PAGE_WB_WAIT")
		)
		page_control_fsm.act("PAGE_WB_WAIT",
			If(self.pagetransferrer.req_complete,
				NextValue(page_dirty[pg_to_writeback], 0),
				NextValue(page_valid[pg_to_writeback], 0),
				If(flush_initiated,
					NextState("FLUSH_DIRTY")
				).Else(
					NextState("PAGE_FETCH_INIT")
				)
			)
		)

		page_control_fsm.act("FLUSH_DIRTY", #1
			NextValue(flush_initiated, 1),
			flush_done.eq(1),
			[If(page_valid[i] & page_dirty[i], NextValue(pg_to_flush, i), flush_done.eq(0)) for i in range(npagesincache)],
			If(flush_done,
				#[NextValue(page_valid[i], 0) for i in range(npagesincache)],
				NextValue(flush_initiated, 0),
				If(flush_all_p, 
					NextState("DONE")
				).Else(
					NextState("TX_FLUSH_DONE")
				)
			).Else(
				NextState("PAGE_WB_INIT")
			)
		)
		

		page_control_fsm.act("RX_CMD", #13
			self.cmd_rx.ack.eq(1),
			If(self.cmd_rx.data_valid,
				self.cmd_rx.data_ren.eq(1),
				If(self.cmd_rx.data[0:32] == 0xF1005,
					NextState("FLUSH_DIRTY")
				).Elif(self.cmd_rx.data[0:32] == 0xC105E,
					NextState("INVALIDATE_ALL_PAGES")
				)
			)
		)
		flush_done_cmd = Signal(128)
		self.comb += flush_done_cmd[64:128].eq(0xD1DF1005D1DF1005)
		page_control_fsm.act("TX_FLUSH_DONE", #14
			self.cmd_tx.start.eq(1),
			self.cmd_tx.len.eq(4),
			self.cmd_tx.last.eq(1),
			If(self.cmd_tx.ack,
				NextState("TX_FLUSH_DONE0")
			)
		)
		for i in range(128//c_pci_data_width):
			page_control_fsm.act("TX_FLUSH_DONE" + str(i), #15
				self.cmd_tx.start.eq(1),
				self.cmd_tx.len.eq(4),
				self.cmd_tx.last.eq(1),
				self.cmd_tx.data.eq(flush_done_cmd[i*c_pci_data_width:(i+1)*c_pci_data_width]),
				self.cmd_tx.data_valid.eq(1),
				If(self.cmd_tx.data_ren,
					NextValue(num_retransmissions, num_retransmissions + 1),
					NextState("TX_FLUSH_DONE" + str(i+1)) 
					if i+1 < 128//c_pci_data_width else 
					If(num_retransmissions < max_retransmissions * 128//c_pci_data_width,
						NextState("TX_FLUSH_DONE")
					).Else(
						NextState("IDLE")
					)
				)
			)
		page_control_fsm.act("DONE", 
			self.done.eq(1),
			NextState("IDLE")
		)
		page_control_fsm.act("INVALIDATE_ALL_PAGES",
			[NextValue(page_valid[i], 0) for i in range(npagesincache)],
			NextState("IDLE")
		)


class VirtmemWrapper(GenericRiffa):
	def __init__(self, combined_interface_rx, combined_interface_tx, c_pci_data_width=32, wordsize=32, ptrsize=64, drive_clocks=True):
		GenericRiffa.__init__(self, combined_interface_rx=combined_interface_rx, combined_interface_tx=combined_interface_tx, c_pci_data_width=c_pci_data_width, drive_clocks=drive_clocks)

		if drive_clocks:
			self.clock_domains.cd_sys = ClockDomain()

		rx0, tx0 = self.get_channel(0)
		rx1, tx1 = self.get_channel(1)
		self.submodules.virtmem = Virtmem(rx0, tx0, rx1, tx1, c_pci_data_width=c_pci_data_width, wordsize=wordsize, ptrsize=ptrsize)

