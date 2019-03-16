# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function
import unittest
import itertools

from .TestUtilities import xfail_without_db
import PyOpenWorm
from PyOpenWorm.context import Context
from PyOpenWorm.neuron import Neuron
from PyOpenWorm.worm import Worm
from PyOpenWorm.evidence import Evidence
from PyOpenWorm.evidence import evidence_for

# XXX: This could probably just be one test at this point -- iterate over all contexts and check for an
# Evidence:supports triple
class EvidenceCoverageTest(unittest.TestCase):
    ''' Tests for statements having an associated Evidence object '''
    def setUp(self):
        xfail_without_db()
        self.conn = PyOpenWorm.connect(configFile='tests/data_integrity_test.conf')
        self.g = self.conn.conf["rdf.graph"]
        self.context = Context()
        self.qctx = self.context.stored

    def tearDown(self):
        PyOpenWorm.disconnect(self.conn)

    def test_verify_neurons_have_evidence(self):
        """
        For each neuron in PyOpenWorm, verify
        that there is supporting evidence
        """

        neurons = list(self.qctx(Neuron)().load())
        evcheck = []
        knowns = dict()
        for n in neurons:
            pp = [x.statements for x in (n.neurotransmitter,
                                         n.type,
                                         n.innexin,
                                         n.neuropeptide)]
            for stmt in itertools.chain(*pp):
                if stmt.context.identifier in knowns:
                    n = self.get_supporting_evidence(stmt)
                    knowns[stmt.context.identifier] = n
                    evcheck.append(n)

        self.assertTrue(0 not in evcheck, "There appears to be no evidence: " + str(evcheck))

    def test_verify_muslces_have_evidence(self):
        """ For each muscle in PyOpenWorm, verify
        that there is supporting evidence"""
        muscles = list(self.qctx(Worm)().muscles())
        evcheck = []
        knowns = dict()
        for n in muscles:
            pp = [x.statements for x in (n.receptors,
                                         n.innervatedBy)]
            for stmt in itertools.chain(*pp):
                if stmt.context.identifier in knowns:
                    n = self.get_supporting_evidence(stmt)
                    knowns[stmt.context.identifier] = n
                    evcheck.append(n)

        self.assertTrue(0 not in evcheck, "There appears to be no evidence: " + str(evcheck))

    @unittest.expectedFailure
    def test_verify_connections_have_evidence(self):
        """ For each connection in PyOpenWorm, verify that there is
        supporting evidence. """
        net = Worm().get_neuron_network()
        connections = list(net.synapses())
        evcheck = []
        for c in connections:
            has_evidence = len(self.get_supporting_evidence(c))
            evcheck.append(has_evidence)

        self.assertTrue(0 not in evcheck)

    @unittest.skip('There is no information at present about channels')
    def test_verify_channels_have_evidence(self):
        """ For each channel in PyOpenWorm, verify that there is
        supporting evidence. """
        pass

    def get_supporting_evidence(self, stmt):
        """ Helper function for checking amount of Evidence.
        Returns list of Evidence supporting fact. """
        ev = self.qctx(Evidence)()
        ev.supports(stmt.context.rdf_object)
        return ev.count()

    def test_evidence_for(self):
        qctx = Context()
        qctx(Neuron)('AVAL').innexin('UNC-7')
        ev_iterable = evidence_for(qctx, self.conn, Evidence, Context)
        self.assertTrue((len(ev_iterable) != 0))
