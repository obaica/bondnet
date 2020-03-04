import itertools
import copy
import logging
from collections.abc import Iterable
import networkx as nx
import networkx.algorithms.isomorphism as iso
from pymatgen.analysis.graphs import _isomorphic
from collections import defaultdict, OrderedDict
from gnn.database.database import MoleculeWrapperFromAtomsAndBonds
from gnn.utils import create_directory, pickle_dump, pickle_load, yaml_dump, expand_path

logger = logging.getLogger(__name__)


class Reaction:
    """
    A reaction that only has one bond break or the type ``A -> B + C``
    (break a bond not in a ring) or ``A -> B`` (break a bond in a ring).

    Args:
        reactants (list): MoleculeWrapper instances
        products (list): MoleculeWrapper instances
        broken_bond (tuple): indices of atoms associated with the broken bond
    """

    # NOTE most methods in this class only works for A->B and A->B+C type reactions

    def __init__(self, reactants, products, broken_bond=None):
        assert len(reactants) == 1, "incorrect number of reactants, should be 1"
        assert 1 <= len(products) <= 2, "incorrect number of products, should be 1 or 2"
        self.reactants = reactants
        # ordered products needed by `group_by_reactant_bond_and_charge(self)`
        # where we use charge as a dict key
        self.products = self._order_molecules(products)
        self._broken_bond = broken_bond

    def get_free_energy(self):
        energy = 0
        for mol in self.reactants:
            energy -= mol.free_energy
        for mol in self.products:
            if mol.free_energy is None:
                return None
            else:
                energy += mol.free_energy
        return energy

    def get_broken_bond(self):
        if self._broken_bond is None:
            if len(self.products) == 1:
                bonds = is_valid_A_to_B_reaction(
                    self.reactants[0], self.products[0], first_only=True
                )
            else:
                bonds = is_valid_A_to_B_C_reaction(
                    self.reactants[0], self.products[0], self.products[1], first_only=True
                )
            if not bonds:
                raise RuntimeError(
                    "invalid reaction (cannot break a reactant bond to get products)"
                )
            # only one element in `bonds` because of `first_only = True`
            self._broken_bond = bonds[0]

        return self._broken_bond

    def get_broken_bond_attr(self):
        """
        Returns a dict of the species and bond order of the broken bond.
        """
        reactant = self.reactants[0]
        u, v = self.get_broken_bond()
        species = [reactant.species[u], reactant.species[v]]
        try:
            # key=0 because of MultiGraph
            order = reactant.graph.get_edge_data(u, v, key=0)["weight"]
        except KeyError:
            order = 0
        return {"species": species, "order": order}

    def atom_mapping(self):
        """
        Find the atom mapping between products and reactant.

        For example, suppose we have reactant

              C 0
             / \
            /___\
           O     N---H
           1     2   3

        products
               C 0
             / \
            /___\
           O     N
           1     2
        and (note the index of H changes it 0)
            H 0

        The function will give use atom mapping:
        [{0:0, 1:1, 2:2}, {0:3}]

        Returns:
            list: each element is a dict mapping the atoms from product to reactant
        """

        # get subgraphs of reactant by breaking the bond
        # if A->B reaction, there is one element in sugraphs
        # if A->B+C reaction, there are two
        bond = self.get_broken_bond()
        original = copy.deepcopy(self.reactants[0].mol_graph)
        original.break_edge(bond[0], bond[1], allow_reverse=True)
        components = nx.weakly_connected_components(original.graph)
        subgraphs = [original.graph.subgraph(c) for c in components]

        # correspondence between products and reaxtant subgrpahs
        N = len(subgraphs)
        if N == 1:
            corr = {0: 0}
        else:
            # product idx as key and reactant subgraph idx as value
            # order matters since mappings[0] (see below) corresponds to first product
            corr = OrderedDict()
            products = [p.mol_graph for p in self.products]

            # If A->B+B reactions, both correspondences are valid. Here either one
            # would suffice

            # implicitly indicates _isomorphic(subgraphs[1], products[1].graph)
            if _isomorphic(subgraphs[0], products[0].graph):
                corr[0] = 0
                corr[1] = 1
            else:
                corr[0] = 1
                corr[1] = 0

        # atom mapping between products and reactant
        mappings = []
        for pidx, fidx in corr.items():
            mp = nx_graph_atom_mapping(self.products[pidx].graph, subgraphs[fidx])
            if mp is None:
                raise RuntimeError(f"cannot find atom mapping for reaction {str(self)}")
            else:
                mappings.append(mp)

        return mappings

    def bond_mapping_by_int_index(self):
        r"""
        Find the bond mapping between products and reactant, using a single index (the
        index of bond in MoleculeWrapper.bonds ) to denote the bond.

        For example, suppose we have reactant

              C 0
           0 / \ 1
            /___\  3   4
           O  2  N---O---H
           1     2   3  4

        products
              C 0
           1 / \ 0
            /___\
           O  2  N
           1     2
        and (note the index of H changes it 0)
              0
            O---H
            0   1
        The function will give the bond mapping:
        [{0:1, 1:0, 2:2}, {0:4}]


        The mapping is done by finding correspondence between atoms indices of reactant
        and products.

        Returns:
            list: each element is a dict mapping the bonds from product to reactant
        """

        # for the same bond, tuple index as key and integer index as value
        reactants_mapping = [
            {
                (i, j): order
                for m in self.reactants
                for order, (i, j, _) in enumerate(m.bonds)
            }
        ]

        # do not use list comprehension because we need to create empty dict for products
        # with not bonds
        products_mapping = []
        for m in self.products:
            mp = {}
            for order, (i, j, _) in enumerate(m.bonds):
                mp[(i, j)] = order
            products_mapping.append(mp)

        # we only have one reactant
        r_mapping = reactants_mapping[0]

        amp = self.atom_mapping()

        bond_mapping = []
        for p_idx, p_mp in enumerate(products_mapping):
            bmp = {}
            for bond, p_order in p_mp.items():
                # atom mapping between product and reactant of the bond
                bond_amp = [amp[p_idx][i] for i in bond]
                r_order = r_mapping[tuple(sorted(bond_amp))]
                bmp[p_order] = r_order
            bond_mapping.append(bmp)

        return bond_mapping

    def bond_mapping_by_tuple_index(self):
        r"""
        Find the bond mapping between products and reactant, using a tuple index (atom
        index) to denote the bond.

        For example, suppose we have reactant

              C 0
           0 / \ 1
            /___\  3   4
           O  2  N---O---H
           1     2   3  4

        products
              C 0
           1 / \ 0
            /___\
           O  2  N
           2     1
        and (note the index of H changes it 0)
              0
            O---H
            0   1
        The function will give the bond mapping:
        [{(0,1):(0,2), (0,2):(0,1), (1,2):(1,2)}, {(0,1):(3,4)}]


        The mapping is done by finding correspondence between atoms indices of reactant
        and products.

        Returns:
            list: each element is a dict mapping the bonds from product to reactant
        """

        atom_mp = self.atom_mapping()

        bond_mapping = []
        for p, amp in zip(self.products, atom_mp):

            # do not use list comprehension because we need to create empty dict for
            # products with not bonds
            bmp = {}
            for i, j, _ in p.bonds:
                # atom mapping between product and reactant of the bond
                b_product = (i, j)
                b_reactant = tuple(sorted([amp[i], amp[j]]))
                bmp[b_product] = b_reactant
            bond_mapping.append(bmp)

        return bond_mapping

    def bond_mapping_by_sdf_int_index(self):
        """
        Bond mapping between products SDF bonds and reactant SDF bonds.

        We do the below to get a mapping between product sdf int index and reactant
        sdf int index:

        product sdf int index
        --> product sdf tuple index
        --> product graph tuple index
        --> reactant graph tuple index
        --> reactant sdf tuple index
        --> reactant sdf int index

        Unlike the atom mapping (where atom index in graph and sdf are the same),
        when sdf file are written, the ordering of bond may change. So we need to do
        this mapping to ensure the correcting between products bonds and reactant bonds.


        Returns:
            list (dict): each dict is the mapping for one product, from sdf bond index
                of product to sdf bond index of reactant
        """
        reactant = self.reactants[0]

        # reactant sdf bond index (tuple) to sdf bond index (interger)
        reactant_index_tuple2int = {
            b: i for i, b in enumerate(reactant.get_sdf_bond_indices())
        }

        # bond mapping between product sdf and reactant sdf
        bond_mapping = []
        product_to_reactant_mapping = self.bond_mapping_by_tuple_index()
        for p, p2r in zip(self.products, product_to_reactant_mapping):

            mp = {}
            # product sdf bond index (list of tuple)
            psb = p.get_sdf_bond_indices()

            # ib: product sdf bond index (int)
            # b: product sdf bond index (tuple)
            for ib, b in enumerate(psb):
                # product graph bond index (tuple)
                pgb = tuple(sorted(p.ob_bond_idx_to_graph_bond_idx(b)))

                # reactant graph bond index (tuple)
                rgb = p2r[pgb]

                # reactant sdf bond index (tuple)
                rsbt = reactant.graph_bond_idx_to_ob_bond_idx(rgb)

                # reactant sdf bond index (int)
                rsbi = reactant_index_tuple2int[rsbt]

                # product sdf bond index (int) to reactant sdf bond index (int)
                mp[ib] = rsbi

            # list of dict, each dict for one product
            bond_mapping.append(mp)

        return bond_mapping

    def as_dict(self):
        d = {
            "reactants": [
                "{}_{}_{}_{}".format(m.formula, m.charge, m.id, m.free_energy)
                for m in self.reactants
            ],
            "products": [
                "{}_{}_{}_{}".format(m.formula, m.charge, m.id, m.free_energy)
                for m in self.products
            ],
            "charge": [m.charge for m in self.reactants + self.products],
            "broken_bond": self.get_broken_bond(),
            "bond_energy": self.get_free_energy(),
        }
        return d

    def __expr__(self):
        if len(self.products) == 1:
            s = "\nA -> B style reaction\n"
        else:
            s = "\nA -> B + C style reaction\n"
        s += "reactants:\n"
        for p in self.reactants:
            s += f"    {p.formula} ({p.charge})\n"
        s += "products:\n"
        for p in self.products:
            s += f"    {p.formula} ({p.charge})\n"

        return s

    def __str__(self):
        return self.__expr__()

    def __eq__(self, other):
        # this assumes all reactions are valid ones, i.e.
        # A -> B and A -> B + B should not be both valid
        self_ids = {m.id for m in self.reactants + self.products}
        other_ids = {m.id for m in other.reactants + other.products}
        return self_ids == other_ids

    @staticmethod
    def _order_molecules(mols):
        """
        Order the molecules according to the below rules (in order):
        1. molecular mass
        2. number of atoms
        3. number of bonds
        4. alphabetical formula
        5. diameter of molecule graph, i.e. largest distance for node to node
        6. charge

        Args:
            mols: a list of molecules.

        Returns:
            A list of ordered molecules.
        """

        def compare(pa, pb, a, b):
            if pa < pb:
                return [a, b]
            elif pa > pb:
                return [b, a]
            else:
                return None

        def order_by_weight(a, b):
            pa = a.weight
            pb = b.weight
            return compare(pa, pb, a, b)

        def order_by_natoms(a, b):
            pa = len(a.atoms)
            pb = len(b.atoms)
            return compare(pa, pb, a, b)

        def order_by_nbonds(a, b):
            pa = len(a.bonds)
            pb = len(b.bonds)
            return compare(pa, pb, a, b)

        def order_by_formula(a, b):
            pa = a.formula
            pb = b.formula
            return compare(pa, pb, a, b)

        def order_by_diameter(a, b):
            try:
                pa = nx.diameter(a.graph)
            except nx.NetworkXError:
                pa = 100000000
            try:
                pb = nx.diameter(b.graph)
            except nx.NetworkXError:
                pb = 100000
            return compare(pa, pb, a, b)

        def order_by_charge(a, b):
            pa = a.charge
            pb = b.charge
            return compare(pa, pb, a, b)

        if len(mols) == 1:
            return mols

        out = order_by_weight(*mols)
        if out is not None:
            return out
        out = order_by_natoms(*mols)
        if out is not None:
            return out
        out = order_by_nbonds(*mols)
        if out is not None:
            return out
        out = order_by_formula(*mols)
        if out is not None:
            return out
        out = order_by_diameter(*mols)
        if out is not None:
            return out
        a, b = mols
        if a.mol_graph.isomorphic_to(b.mol_graph):
            out = order_by_charge(*mols)  # e.g. H+ and H-
            if out is not None:
                return out
            else:
                return mols  # two exactly the same molecules
        raise RuntimeError("Cannot order molecules")


class ReactionsGroup:
    """
    A group of reactions that have the same reactant.

    This is a base class, so use the derived class.
    """

    def __init__(self, reactant, reactions=None):
        self._reactant = reactant
        self._reactions = []
        if reactions is not None:
            self.add(reactions)

    @property
    def reactant(self):
        return self._reactant

    @property
    def reactions(self):
        return self._reactions

    def add(self, reactions):
        """
        Add one or more reactions to the collection.

        Args:
            reactions: A `Reaction` or a sequence of `Reaction`.

        """
        if isinstance(reactions, Iterable):
            for rxn in reactions:
                self._add_one(rxn)
        else:
            self._add_one(reactions)

    def _add_one(self, rxn):
        if rxn.reactants[0] != self.reactant:
            raise ValueError(
                "Cannot add reaction whose reactant is different from what already "
                "in the collection."
            )
        self._reactions.append(rxn)


class ReactionsOfSameBond(ReactionsGroup):
    """
    A collection of reactions associated with the same bond in the reactant.

    This is mainly to consider products with the same mol graph (i.e. isomorphic to
    each other) but different charges.
    """

    def __init__(self, reactant, reactions=None, broken_bond=None):
        super(ReactionsOfSameBond, self).__init__(reactant, reactions)
        self._broken_bond = broken_bond

    @property
    def broken_bond(self):
        if self._broken_bond is None:
            if self._reactions:
                self._broken_bond = self.reactions[0].get_broken_bond()
            else:
                raise RuntimeError(
                    "Cannot get broken bond. You can either provide it at instantiation "
                    "or add some reactions and then try again."
                )
        return self._broken_bond

    def create_complement_reactions(self):
        """
        Create reactions to complement the ones present in the database such that each
        bond has reactions of all combination of charges.

        For example, if we have `A (0) -> B (0) + C (0)` and `A (0) -> B (1) + C (-1)`
        in the database, this will create reaction `A (0) -> B (-1) + C (1)`,
        assuming molecule charges {-1,0,1} are allowed.

        Returns:
            A list of Reactions.
        """

        def factor_integer(x, low, high, num=2):
            """
            Factor an integer to the sum of multiple integers that with in the
            range of [low, high].

            Args:
                x (int): the integer to be factored
                num (int): number of integers to sum

            Returns:
                set: factor values

            Example:
                >>> factor_integer(0, 1, -1, 2)
                >>> [(-1, 1), (0, 0), (1, -1)]
            """
            if num == 1:
                return {(x)}

            elif num == 2:
                res = []
                for i, j in itertools.product(range(low, high + 1), repeat=2):
                    if i + j == x:
                        res.append((i, j))

                return set(res)
            else:
                raise Exception(f"not implemented for num={num} case.")

        # find products charges
        fragments = self.reactant.fragments[self.broken_bond]
        N = len(fragments)

        # A -> B reaction
        if N == 1:
            # For N = 1 case, there could only be one reaction where the charges of
            # the reactant and product are the same. If we already have one reaction,
            # no need to find the complementary one.
            if len(self.reactions) == 1:
                return []
            else:
                missing_charge = [[self.reactant.charge]]

        # N == 2 case, i.e. A -> B + C reaction (B could be the same as C)
        else:
            target_products_charge = factor_integer(
                self.reactant.charge, low=-1, high=1, num=N
            )

            # all possible reactions are present
            if len(target_products_charge) == len(self.reactions):
                return []
            else:
                products_charge = []
                for rxn in self.reactions:
                    x = len(self.reactions)
                    products = [p.mol_graph for p in rxn.products]
                    charge = [p.charge for p in rxn.products]

                    # A->B+B reaction
                    if products[0].isomorphic_to(products[1]):
                        products_charge.append((charge[0], charge[1]))
                        products_charge.append((charge[1], charge[0]))

                    # A->B+C reaction
                    else:
                        # implicitly indicates fragments[1].isomorphic_to(products[1])
                        if fragments[0].isomorphic_to(products[0]):
                            products_charge.append((charge[0], charge[1]))
                        else:
                            products_charge.append((charge[1], charge[0]))

                missing_charge = target_products_charge - set(products_charge)

        # fragments species, coords, and bonds (same for products)
        species = []
        coords = []
        bonds = []
        for fg in fragments:
            nodes = fg.graph.nodes.data()
            nodes = sorted(nodes, key=lambda pair: pair[0])
            species.append([v["specie"] for k, v in nodes])
            coords.append([v["coords"] for k, v in nodes])
            edges = fg.graph.edges.data()
            bonds.append([(i, j) for i, j, v in edges])

        # create complementary reactions
        bb = self.broken_bond
        comp_rxns = []
        for charge in missing_charge:
            products = []
            for i, c in enumerate(charge):
                mid = f"{self.reactant.id}-{bb[0]}-{bb[1]}-{i}-{c}"
                products.append(
                    MoleculeWrapperFromAtomsAndBonds(
                        species[i], coords[i], c, bonds[i], mol_id=mid
                    )
                )
            rxn = Reaction([self.reactant], products, broken_bond=bb)
            comp_rxns.append(rxn)

        return comp_rxns

    def order_reactions(self, complement_reactions=False):
        """
        Order reactions by energy.

        If complement reactions (whose energy is `None`) are used, they are placed
        after reactions having energies.

        Args:
            complement_reactions (bool): If `False`, order the existing reactions only.
                Otherwise, complementary reactions are created and ordered together
                with existing ones.

        Returns:
            list: a sequence of :class:`Reaction` ordered by energy
        """
        ordered_rxns = sorted(self.reactions, key=lambda rxn: rxn.get_free_energy())
        if complement_reactions:
            comp_rxns = self.create_complement_reactions()
            ordered_rxns += comp_rxns
        return ordered_rxns


class ReactionsOnePerBond(ReactionsGroup):
    """
    A collection of reactions for the same reactant.
    There is at most one reaction associated with a bond, either a specific choice
    of charges (e.g. 0 -> 0 + 0) or lowest energy reaction across charges.
    """

    def _add_one(self, rxn):
        if rxn.reactants[0] != self.reactant:
            raise ValueError(
                "Cannot add reaction whose reactant is different from what already in "
                "the collection."
            )
        bond = rxn.get_broken_bond()
        for r in self.reactions:
            if r.get_broken_bond() == bond:
                raise ValueError(
                    f"Reaction breaking bond {bond} already exists.\n"
                    f"Existing reaction: {str(r.as_dict())}\n"
                    f"New      reaction: {str(rxn.as_dict())}"
                )
        self._reactions.append(rxn)

    def order_reactions(self, complement_reactions=True):
        """
        Order the reactions by charge.

        Args:
            complement_reactions (bool): If `False`, order the existing reactions only.
                Otherwise, complementary reactions are created and ordered together
                with existing ones.

        Returns:
            dict of dict: The outer dict has bond indices (a tuple) as the key and the
                inner dict have keys `energy`, `reaction`, `order`. Their values are
                set to `None` if there is no reaction associated with the bond.
        """

        ordered_rxns = sorted(self.reactions, key=lambda rxn: rxn.get_free_energy())
        ordered_rxns_dict = dict()
        for i, (bond, rxn) in enumerate(ordered_rxns):
            ordered_rxns_dict[bond] = {
                "reaction": rxn,
                "order": i,
                "energy": rxn.get_free_energy(),
            }

        if complement_reactions:
            bonds_of_existing_rxns = [bond for bond, rxn in ordered_rxns]
            for i, j, attr in self.reactant.bonds:
                bond = (i, j)
                if bond not in bonds_of_existing_rxns:
                    ordered_rxns_dict[bond] = {
                        "reaction": None,
                        "order": None,
                        "energy": None,
                    }

        return ordered_rxns_dict


class ReactionsMultiplePerBond(ReactionsGroup):
    """
    A collection of reactions for the same reactant.

    Each bond can be associated with mutiple reactions of different charges.
    """

    def group_by_bond(self, find_one=True):
        """
        Group reactions with same broken bond together.

        If there is not reactions associated with a bond of the reactant, the
        corresponding :class:`ReactionsOfSameBond` is still created, but initialized
        with empty reactions.

        Args:
            find_one (bool): If `True`, keep one reaction for each isomorphic bond
                group. If `False`, keep all.
                Note, if set to `True`, this expects that `find_one=False` in
                :method:`ReactionExtractor..extract_one_bond_break` so that all bonds
                in an isomorphic group have exactly the same reactions.
                In such, we just need to retain a random bond and its associated
                reactions in each group.

        Returns:
            list: a sequence of :class:`ReactionsOfSameBond`, one for each bond of the
            reactant.
        """

        # init an empty [] for each bond
        # doing this instead of looping over self.reactions ensures bonds without
        # reactions are correctly represented
        bond_rxns_dict = OrderedDict()
        for i, j, _ in self.reactant.bonds:
            bond = (i, j)
            bond_rxns_dict[bond] = []
        # assign rxn to bond group
        for rxn in self.reactions:
            bond_rxns_dict[rxn.get_broken_bond()].append(rxn)

        # remove duplicate isomorphic bonds
        if find_one:
            for group in self.reactant.isomorphic_bonds:
                # keep the first bond in each group and remove others
                for i in range(1, len(group)):
                    bond_rxns_dict.pop(group[i])

        # create ReactionsOfSameBond instance
        reactions = []
        for bond, rxns in bond_rxns_dict.items():
            rsb = ReactionsOfSameBond(self.reactant, reactions=rxns, broken_bond=bond)
            reactions.append(rsb)

        return reactions

    def order_reactions(self, complement_reactions=False, one_per_iso_bond_group=True):
        """
        Order reactions by energy.

        If complement reactions (whose energy is `None`) are used, they are placed
        after reactions having energies.

        Args:
            complement_reactions (bool): If `False`, order the existing reactions only.
                Otherwise, complementary reactions are created and ordered together
                with existing ones.
            one_per_iso_bond_group (bool): If `True`, keep one reaction for each
                isomorphic bond group. If `False`, keep all.
                Note, if set to `True`, this expects that `find_one=False` in
                :method:`ReactionExtractor.extract_one_bond_break` so that all bonds
                in an isomorphic bond group have exactly the same reactions.

        Returns:
            list: a sequence of :class:`Reaction` ordered by energy
        """

        # NOTE, we need to get existing_rxns from rsb instead of self.reactions because
        # we may need to remove duplicate isomorphic bond, which is handled in
        # self.group_by_bonds.

        existing_rxns = []
        comp_rxns = []
        rsb_group = self.group_by_bond(find_one=one_per_iso_bond_group)
        for rsb in rsb_group:
            existing_rxns.extend(rsb.reactions)
            comp_rxns.extend(rsb.create_complement_reactions())

        # sort reactions we have energy for
        ordered_rxns = sorted(existing_rxns, key=lambda rxn: rxn.get_free_energy())

        if complement_reactions:
            ordered_rxns += comp_rxns

        return ordered_rxns


class ReactionExtractor:
    def __init__(self, molecules, reactions=None):
        self.molecules = molecules or self._get_molecules_from_reactions(reactions)
        self.reactions = reactions

    def bucket_molecules(self, keys=["formula", "charge"]):
        """
        Classify molecules into nested dictionaries according to molecule properties
        specified in ``keys``.

        Args:
            keys (list of str): each str should be a molecule property.

        Returns:
            nested dictionary of molecules classified according to keys.
        """
        logger.info("Start bucketing molecules...")

        num_keys = len(keys)
        buckets = {}
        for m in self.molecules:
            b = buckets
            for i, k in enumerate(keys):
                v = getattr(m, k)
                if i == num_keys - 1:
                    b.setdefault(v, []).append(m)
                else:
                    b.setdefault(v, {})
                b = b[v]

        return buckets

    def extract_A_to_B_style_reaction(self, find_one=True):
        """
        Extract a list of A -> B reactions.

        Args:
            find_one (bool): For a given reactant A and product B, there could be
                multiple reactions between them (breaking different bonds of reactant
                results in the same product). If `True`, for each A and B only get
                one reaction between them. If `False` get all.

        Returns:
            list: a sequence of :class:`Reaction`.
        """

        logger.info("Start extracting A -> B style reactions")

        buckets = self.bucket_molecules(keys=["formula", "charge"])

        A2B = []
        i = 0
        for formula in buckets:
            i += 1
            if i % 10000 == 0:
                logger.info(f"A -> B running bucket {i}")

            for charge in buckets[formula]:
                for A, B in itertools.permutations(buckets[formula][charge], 2):
                    bonds = is_valid_A_to_B_reaction(A, B, first_only=find_one)
                    for b in bonds:
                        A2B.append(Reaction([A], [B], b))

        self.reactions = A2B

        logger.info("{} A -> B style reactions extracted".format(len(A2B)))

        return A2B

    def extract_A_to_B_C_style_reaction(self, find_one=True):
        """
        Extract a list of A -> B + C reactions (B and C can be the same molecule).

        Args:
            find_one (bool): For a given reactant A and product B, there could be
                multiple reactions between them (breaking different bonds of reactant
                results in the same product). If `True`, for each A and B only get
                one reaction between them. If `False` get all.

        Returns:
            list: a sequence of :class:`Reaction`.
        """

        logger.info("Start extracting A -> B + C style reactions")

        buckets = self.bucket_molecules(keys=["formula", "charge"])

        fcmap = self._get_formula_composition_map(self.molecules)

        A2BC = []
        i = 0
        for formula_A in buckets:
            for formula_B, formula_C in itertools.combinations_with_replacement(
                buckets, 2
            ):
                i += 1
                if i % 10000 == 0:
                    logger.info(f"A -> B + C running bucket {i}")

                if not self._is_valid_A_to_B_C_composition(
                    fcmap[formula_A], fcmap[formula_B], fcmap[formula_C]
                ):
                    continue

                reaction_ids = []
                for (charge_A, charge_B, charge_C) in itertools.product(
                    buckets[formula_A], buckets[formula_B], buckets[formula_C]
                ):
                    if not self._is_valid_A_to_B_C_charge(charge_A, charge_B, charge_C):
                        continue

                    for A, B, C in itertools.product(
                        buckets[formula_A][charge_A],
                        buckets[formula_B][charge_B],
                        buckets[formula_C][charge_C],
                    ):

                        # exclude reactions already considered
                        # Since we use `combinations_with_replacement` to consider
                        # products B and C for the same formula, buckets[formula_B] and
                        # buckets[C] could be the same buckets. Then when we use
                        # itertools.product to loop over them, molecules (M, M') could
                        # be either (B, C) or (C, B), appearing twice.
                        # We can do combinations_with_replacement for the loop over
                        # charge when formula_B and formula_C are the same, but this
                        # would complicate the code. So we use reaction_ids to keep
                        # record and not include them.
                        ids = {A.id, B.id, C.id}
                        if ids in reaction_ids:
                            continue

                        bonds = is_valid_A_to_B_C_reaction(A, B, C, first_only=find_one)
                        if bonds:
                            reaction_ids.append(ids)
                            for b in bonds:
                                A2BC.append(Reaction([A], [B, C], b))

        self.reactions = A2BC

        logger.info("{} A -> B + C style reactions extracted".format(len(A2BC)))

        return A2BC

    def extract_one_bond_break(self, find_one=True):
        """
        Extract all reactions that only has one bond break or the type ``A -> B + C``
        (break a bond not in a ring) or ``A -> D`` (break a bond in a ring)

        Returns:
            A list of reactions.
        """
        A2B = self.extract_A_to_B_style_reaction(find_one)
        A2BC = self.extract_A_to_B_C_style_reaction(find_one)
        self.reactions = A2B + A2BC

        return A2B, A2BC

    def filter_reactions_by_reactant_attribute(self, key, values):
        """
        Filter the reactions by the `key` of reactant, and only reactions the attribute of
        the of `key` is in `values` are retained.

        Args:
            key (str): attribute of readtant
            values (list): list of allowable values
        """
        reactions = []
        for rxn in self.reactions:
            if getattr(rxn.reactants[0], key) in values:
                reactions.append(rxn)

        self.molecules = self._get_molecules_from_reactions(reactions)
        self.reactions = reactions

    def filter_reactions_by_bond_type_and_order(self, bond_type, bond_order=None):
        """
        Filter the reactions by the type of the breaking bond, and only reactions with the
        specified bond_type will be retained.

        Args:
            bond_type (tuple of string): species of the two atoms the bond connecting to
            bond_order (int): bond order to filter on
        """
        reactions = []
        for rxn in self.reactions:
            attr = rxn.get_broken_bond_attr()
            species = attr["species"]
            order = attr["order"]
            if set(species) == set(bond_type):
                if bond_order is None:
                    reactions.append(rxn)
                else:
                    if order == bond_order:
                        reactions.append(rxn)

        self.molecules = self._get_molecules_from_reactions(reactions)
        self.reactions = reactions

    def sort_reactions_by_reactant_formula(self):
        self.reactions = sorted(self.reactions, key=lambda rxn: rxn.reactants[0].formula)

    def group_by_reactant(self):
        """
        Group reactions that have the same reactant together.

        Returns:
            dict: with reactant as the key and list of :class:`Reaction` as the value
        """
        grouped_reactions = defaultdict(list)
        for rxn in self.reactions:
            reactant = rxn.reactants[0]
            grouped_reactions[reactant].append(rxn)
        return grouped_reactions

    def group_by_reactant_charge_0(self):
        """
        Group reactions that have the same reactant together, keeping charge 0
        reactions (charges of reactant and products are all 0).

        A group of reactions of the same reactant are put in to
        :class:`ReactionsOnePerBond` container.

        Returns:
            list: a sequence of :class:`ReactionsOnePerBond`
        """
        groups = self.group_by_reactant()

        new_groups = []
        for reactant in groups:

            zero_charge_rxns = []
            for rxn in groups[reactant]:
                zero_charge = True
                for m in rxn.reactants + rxn.products:
                    if m.charge != 0:
                        zero_charge = False
                        break
                if zero_charge:
                    zero_charge_rxns.append(rxn)

            # add to new group only when at least has one reaction
            if zero_charge_rxns:
                rsr = ReactionsOnePerBond(reactant, zero_charge_rxns)
                new_groups.append(rsr)

        return new_groups

    def group_by_reactant_lowest_energy(self):
        """
        Group reactions that have the same reactant together.

        For reactions that have the same reactant and breaks the same bond, we keep the
        reaction that have the lowest energy across products charge.

        A group of reactions of the same reactant are put in to
        :class:`ReactionsOnePerBond` container.

        Returns:
            list: a sequence of :class:`ReactionsOnePerBond`
        """

        groups = self.group_by_reactant()

        new_groups = []
        for reactant in groups:

            # find the lowest energy reaction for each bond
            lowest_energy_reaction = dict()
            for rxn in groups[reactant]:
                bond = rxn.get_broken_bond()
                if bond not in lowest_energy_reaction:
                    lowest_energy_reaction[bond] = rxn
                else:
                    e_old = lowest_energy_reaction[bond].get_free_energy()
                    e_new = rxn.get_free_energy()
                    if e_new < e_old:
                        lowest_energy_reaction[bond] = rxn

            rsr = ReactionsOnePerBond(reactant, lowest_energy_reaction.keys())
            new_groups.append(rsr)

        return new_groups

    def group_by_reactant_all(self):
        """
        Group reactions that have the same reactant together.

        A group of reactions of the same reactant are put in to
        :class:`ReactionsMultiplePerBond` container.

        Returns:
            list: a sequence of :class:`ReactionsMultiplePerBond`
        """

        groups = self.group_by_reactant()
        new_groups = [
            ReactionsMultiplePerBond(reactant, rxns) for reactant, rxns in groups.items()
        ]

        return new_groups

    def group_by_reactant_charge_pair(self):
        """
        Group reactions whose reactants are isomorphic to each other but have
        different charges.

        Then create pairs of reactions where the reactant and products of one reaction is
        is isomorphic to those of the other reaction in a pair. The pair is indexed by
        the charges of the reactants of the pair.

        Returns:
            A dict with a type (charge1, charge2) as the key, and a list of tuples as
            the value, where each tuple are two reactions (reaction1, reactions2) that
            have the same breaking bond.
        """

        grouped_reactions = self.group_by_reactant_lowest_energy()

        # groups is a list of list, where the elements of each inner list are
        # ReactionsOnePerBond instances and the corresponding reactants are
        # isomorphic to each other
        groups = []
        for rsr in grouped_reactions:
            find_iso = False
            for g in groups:
                old_rsr = g[0]
                # add to the isomorphic group
                if rsr.reactant.mol_graph.isomorphic_to(old_rsr.reactant.mol_graph):
                    g.append(rsr)
                    find_iso = True
                    break
            if not find_iso:
                g = [rsr]
                groups.append(g)

        # group by charge of a pair of reactants
        result = defaultdict(list)
        for g in groups:
            for rsr1, rsr2 in itertools.combinations(g, 2):
                if rsr2.reactant.charge < rsr1.reactant.charge:
                    rsr1, rsr2 = rsr2, rsr1
                rxn1 = {r.get_broken_bond(): r for r in rsr1.reactions}
                rxn2 = {r.get_broken_bond(): r for r in rsr2.reactions}
                res = get_same_bond_breaking_reactions_between_two_reaction_groups(
                    rsr1.reactant, rxn1, rsr2.reactant, rxn2
                )
                result[(rsr1.reactant.charge, rsr2.reactant.charge)].extend(res)
        return result

    def get_reactions_with_0_charge(self):
        """
        Get reactions the charges of reactant and products are all 0.

        Returns:
            list: a sequence of :class:`Reaction`.
        """
        groups = self.group_by_reactant_charge_0()
        reactions = []
        for rsr in groups:
            reactions.extend(rsr.reactions)
        return reactions

    def get_reactions_with_lowest_energy(self):
        """
        Get the reactions by removing higher energy ones. Higher energy is compared
        across product charge.

        Returns:
            list: a sequence of :class:`Reaction`.
        """
        groups = self.group_by_reactant_lowest_energy()
        reactions = []
        for rsr in groups:
            reactions.extend(rsr.reactions)
        return reactions

    def write_bond_energies(self, filename):
        for m in self.molecules:
            m.make_picklable()

        groups = self.group_by_reactant_all()

        # convert to nested dict: new_groups[reactant_idx][bond][charge] = rxn
        new_groups = OrderedDict()
        for rmb in groups:
            m = rmb.reactant
            key = "{}_{}_{}_{}".format(m.formula, m.charge, m.id, m.free_energy)
            new_groups[key] = OrderedDict()
            rsbs = rmb.group_by_bond()
            for rsb in rsbs:
                bond = rsb.broken_bond
                new_groups[key][bond] = OrderedDict()
                for rxn in rsb.reactions:
                    charge = tuple([m.charge for m in rxn.products])
                    new_groups[key][bond][charge] = rxn.as_dict()

        yaml_dump(new_groups, filename)

    def create_struct_label_dataset_reaction_based(
        self,
        struct_file="sturct.sdf",
        label_file="label.txt",
        feature_file=None,
        complement_reactions=False,
        one_per_iso_bond_group=True,
        top_n=2,
    ):

        all_mols = []
        all_labels = []  # one per reaction

        rmb_list = self.group_by_reactant_all()

        # rmb: all reactions for a reactant
        for rmb in rmb_list:
            reactions = rmb.order_reactions(complement_reactions, one_per_iso_bond_group)

            # rxn: a reaction for one bond and a specific combination of charges
            for i, rxn in enumerate(reactions):
                mols = rxn.reactants + rxn.products
                energy = rxn.get_free_energy()
                if energy is not None:
                    if i < top_n:
                        lb = 0
                    else:
                        lb = 1
                else:
                    lb = 2

                # bond mapping between product sdf and reactant sdf
                all_mols.extend(mols)
                data = {
                    "class": lb,
                    "num_mols": len(mols),
                    "atom_mapping": rxn.atom_mapping(),
                    "bond_mapping": rxn.bond_mapping_by_sdf_int_index(),
                }
                all_labels.append(data)

        # write sdf
        self.write_sdf(all_mols, struct_file)

        # label file
        yaml_dump(all_labels, label_file)

        # write feature
        if feature_file is not None:
            self.write_feature(all_mols, bond_indices=None, filename=feature_file)

    def create_struct_label_dataset_bond_based_classification(
        self,
        struct_file="sturct.sdf",
        label_file="label.txt",
        feature_file=None,
        lowest_across_product_charge=True,
        top_n=2,
    ):
        """
        Write the reaction class to files.

        Also, this is based on the bond energy, i.e. each bond (that we have energies)
        will have one line in the label file.

        Args:
            struct_file (str): filename of the sdf structure file
            label_file (str): filename of the label
            feature_file (str): filename for the feature file, if `None`, do not write it
            lowest_across_product_charge (bool): If `True` each reactant corresponds to
                the lowest energy products. If `False`, find all 0->0+0 reactions,
                i.e. the charge of reactant and products should all be zero.

        """

        def write_label(reactants, bond_idx, label_class, filename="label.txt"):
            """
            Write bond energy class to file.

            See the text below on how the info is written.

            Args:
                reactants (list ): MoleculeWrapper objects
                bond_idx (list of int): the index of the broken bond in the reactant;
                filename (str): name of the file to write the label
            """

            filename = expand_path(filename)
            create_directory(filename)
            with open(filename, "w") as f:
                f.write(
                    "# Each line lists the energy class of a bond in a molecule. "
                    "Each line has three items: "
                    "1st: an integer of of {0,1,2}, indicating the class of bond energy, "
                    "0 stands for feasible reaction, 1 stands for nonfeasize reaction "
                    "and 2 stands for unknown, i.e. we do not have info about the "
                    "reaction."
                    "2nd: index of the bond in the molecule {0,1,2,num_bonds-1}."
                    "3rd: molecule idx from which the bond come.\n"
                )

                for i, (m, idx, lb) in enumerate(zip(reactants, bond_idx, label_class)):
                    f.write("{} {} {}\n".format(lb, idx, m.id))

        if lowest_across_product_charge:
            grouped_reactions = self.group_by_reactant_lowest_energy()
        else:
            grouped_reactions = self.group_by_reactant_charge_0()

        all_reactants = []
        broken_bond_idx = []  # int index in ob molecule
        broken_bond_pairs = []  # a tuple index in graph molecule
        label_class = []
        for rsr in grouped_reactions:
            reactant = rsr.reactant
            ordered_reactions = rsr.order_reactions()

            # bond energies in the same order as in sdf file
            sdf_bonds = reactant.get_sdf_bond_indices()
            for ib, bond in enumerate(sdf_bonds):
                # change index from ob to graph
                bond = tuple(sorted(reactant.ob_bond_idx_to_graph_bond_idx(bond)))
                data = ordered_reactions[bond]

                # NOTE this will only write class 0 and class 1
                # rxn = data["reaction"]
                # if rxn is None:  # do not have reaction breaking bond
                #     continue

                order = data["order"]
                if order is None:
                    lb = 2
                elif order < top_n:
                    lb = 0
                else:
                    lb = 1
                all_reactants.append(reactant)
                broken_bond_idx.append(ib)
                broken_bond_pairs.append(bond)
                label_class.append(lb)

        # write label
        write_label(all_reactants, broken_bond_idx, label_class, label_file)

        # write sdf
        self.write_sdf(all_reactants, struct_file)

        # write feature
        if feature_file is not None:
            self.write_feature(all_reactants, broken_bond_pairs, filename=feature_file)

    def create_struct_label_dataset_bond_based_regressssion(
        self,
        struct_file="sturct.sdf",
        label_file="label.txt",
        feature_file=None,
        lowest_across_product_charge=True,
    ):
        """
        Write the reactions to files.

        Also, this is based on the bond energy, i.e. each bond (that we have energies)
        will have one line in the label file.

        args:
            struct_file (str): filename of the sdf structure file
            label_file (str): filename of the label
            feature_file (str): filename for the feature file, if `None`, do not write it
            lowest_across_product_charge (bool): If `True` each reactant corresponds to
                the lowest energy products. If `False`, find all 0->0+0 reactions,
                i.e. the charge of reactant and products should all be zero.

        """

        def write_label(reactions, bond_idx, filename="label.txt"):
            """
            Write bond energy to file.

            See the text below on how the info is written.

            Args:
                reactions (list of Reaction):
                bond_idx (list of int): the index of the broken bond in the reactant;
                filename (str): name of the file to write the label
            """

            filename = expand_path(filename)
            create_directory(filename)
            with open(filename, "w") as f:
                f.write(
                    "# Each line lists the energy of a bond in a molecule. "
                    "The number of items in each line is equal to 2*N+1, where N is the "
                    "number bonds in the molecule. The first N items are bond energies "
                    "and the next N items are indicators (0 or 1) specifying whether the "
                    "bond energy exists. A value of 0 means the corresponding bond "
                    "energy should be ignored, whatever its value is. The last item "
                    "specifies the molecule from which the bond come.\n"
                )

                for i, (rxn, idx) in enumerate(zip(reactions, bond_idx)):
                    reactant = rxn.reactants[0]
                    num_bonds = len(reactant.bonds)

                    # write bond energies
                    for j in range(num_bonds):
                        if j == idx:
                            f.write("{:.15g} ".format(rxn.get_free_energy()))
                        else:
                            f.write("0.0 ")
                    f.write("   ")

                    # write bond energy indicator
                    for j in range(num_bonds):
                        if j == idx:
                            f.write("1 ")
                        else:
                            f.write("0 ")

                    # write which molecule this atom come from
                    f.write("    {}".format(reactant.id))

                    # write other info (reactant and product info, and bond energy)

                    attr = rxn.as_dict()
                    f.write(
                        "    # {} {} {} {}\n".format(
                            attr["reactants"],
                            attr["products"],
                            reactant.graph_bond_idx_to_ob_bond_idx(attr["broken_bond"]),
                            attr["bond_energy"],
                        )
                    )

        if lowest_across_product_charge:
            grouped_reactions = self.group_by_reactant_lowest_energy()
        else:
            grouped_reactions = self.group_by_reactant_charge_0()

        all_rxns = []
        broken_bond_idx = []
        broken_bond_pairs = []
        for rsr in grouped_reactions:
            reactant = rsr.reactant

            # bond energies in the same order as in sdf file
            sdf_bonds = reactant.get_sdf_bond_indices()
            for ib, bond in enumerate(sdf_bonds):
                # change index from ob to graph
                bond = tuple(sorted(reactant.ob_bond_idx_to_graph_bond_idx(bond)))
                data = rsr.order_reactions()[bond]
                rxn = data["reaction"]

                if rxn is None:  # do not have reaction breaking bond
                    continue

                all_rxns.append(rxn)
                broken_bond_idx.append(ib)
                broken_bond_pairs.append(bond)

        all_reactants = [rxn.reactants[0] for rxn in all_rxns]

        # write label
        write_label(all_rxns, broken_bond_idx, label_file)

        # write sdf
        self.write_sdf(all_reactants, struct_file)

        # write feature
        if feature_file is not None:
            self.write_feature(all_reactants, broken_bond_pairs, filename=feature_file)

    def create_struct_label_dataset_mol_based(
        self,
        struct_file="sturct.sdf",
        label_file="label.txt",
        feature_file=None,
        lowest_across_product_charge=True,
    ):
        """
        Write the reactions to files.

        The is molecule based, each molecule will have a line in the label file.

        args:
            struct_file (str): filename of the sdf structure file
            label_file (str): filename of the label
            feature_file (str): filename for the feature file, if `None`, do not write it
            lowest_across_product_charge (bool): If `True` each reactant corresponds to
                the lowest energy products. If `False`, find all 0->0+0 reactions,
                i.e. the charge of reactant and products should all be zero.

        """
        if lowest_across_product_charge:
            grouped_reactions = self.group_by_reactant_lowest_energy()
        else:
            grouped_reactions = self.group_by_reactant_charge_0()

        # write label
        label_file = expand_path(label_file)
        create_directory(label_file)
        with open(label_file, "w") as f:
            f.write(
                "# Each line lists the bond energies of a molecule. "
                "The number of items in each line is equal to 2*N, where N is the "
                "number bonds. The first N items are bond energies and the next N "
                "items are indicators (0 or 1) to specify whether the bond energy "
                "exists in the dataset. A value of 0 means the corresponding bond "
                "energy should be ignored, whatever its value is.\n"
            )

            for rsr in grouped_reactions:
                reactant = rsr.reactant

                # get a mapping between babel bond and reactions
                rxns_by_ob_bond = dict()
                for rxn in rsr.reactions:
                    bond = rxn.get_broken_bond()
                    bond = tuple(sorted(reactant.graph_bond_idx_to_ob_bond_idx(bond)))
                    # we need this because the order of bond changes in sdf file
                    rxns_by_ob_bond[bond] = rxn

                # write bond energies in the same order as sdf file
                energy = []
                indicator = []
                sdf_bonds = reactant.get_sdf_bond_indices()
                for ib, bond in enumerate(sdf_bonds):

                    if bond in rxns_by_ob_bond:  # have reaction with breaking this bond
                        rxn = rxns_by_ob_bond[bond]
                        energy.append(rxn.get_free_energy())
                        indicator.append(1)
                    else:
                        energy.append(0.0)
                        indicator.append(0)

                for i in energy:
                    f.write("{:.15g} ".format(i))
                f.write("    ")
                for i in indicator:
                    f.write("{} ".format(i))
                f.write("\n")

        # write sdf
        reactants = [rsr.reactant for rsr in grouped_reactions]
        self.write_sdf(reactants, struct_file)

        # write feature
        # we just need one reaction for each group with the same reactant
        rxns = [rsr.reactions[0] for rsr in grouped_reactions]
        if feature_file is not None:
            self.write_feature(rxns, bond_indices=None, filename=feature_file)

    @staticmethod
    def write_sdf(molecules, filename="molecules.sdf"):
        """
        Write molecules sdf to file.

        Args:
            filename (str): output filename
            molecules (list): a sequence of :class:`MoleculeWrapper`
        """
        logger.info("Start writing sdf file: {}".format(filename))

        filename = expand_path(filename)
        create_directory(filename)
        with open(filename, "w") as f:
            for i, m in enumerate(molecules):
                msg = "{}_{}_{}_{} int_id: {}".format(
                    m.formula, m.charge, m.id, m.free_energy, i
                )
                # The same pybel mol will write different sdf file when it is called
                # the first time and other times. We create a new one by setting
                # `_ob_adaptor` to None here so that it will write the correct one.
                m._ob_adaptor = None
                sdf = m.write(file_format="sdf", message=msg)
                f.write(sdf)

        logger.info("Finish writing sdf file: {}".format(filename))

    @staticmethod
    def write_feature(molecules, bond_indices=None, filename="feature.yaml"):
        """
        Write molecules features to file.

        Args:
            molecules (list): a sequence of :class:`MoleculeWrapper`
            bond_indices (list of tuple or None): broken bond in the corresponding
                molecule
            filename (str): output filename
        """
        logger.info("Start writing feature file: {}".format(filename))

        all_feats = []
        for i, m in enumerate(molecules):
            if bond_indices is None:
                idx = None
            else:
                idx = bond_indices[i]
            feat = m.pack_features(use_obabel_idx=True, broken_bond=idx)
            all_feats.append(feat)
        yaml_dump(all_feats, filename)

        logger.info("Finish writing feature file: {}".format(filename))

    @staticmethod
    def _get_formula_composition_map(mols):
        fcmap = dict()
        for m in mols:
            fcmap[m.formula] = m.composition_dict
        return fcmap

    @staticmethod
    def _is_even_composition(composition):
        for _, amt in composition.items():
            if int(amt) % 2 != 0:
                return False
        return True

    @staticmethod
    def _is_valid_A_to_B_C_composition(composition1, composition2, composition3):
        combined23 = defaultdict(int)
        for k, v in composition2.items():
            combined23[k] += v
        for k, v in composition3.items():
            combined23[k] += v
        return composition1 == combined23

    @staticmethod
    def _get_molecules_from_reactions(reactions):
        mols = []
        for r in reactions:
            mols.extend(r.reactants + r.products)
        return list(set(mols))

    @staticmethod
    def _is_valid_A_to_B_C_charge(charge1, charge2, charge3):
        return charge1 == charge2 + charge3

    def to_file(self, filename="rxns.pkl"):
        logger.info("Start writing reactions to file: {}".format(filename))

        for m in self.molecules:
            m.make_picklable()
        d = {
            "@module": self.__class__.__module__,
            "@class": self.__class__.__name__,
            "molecules": self.molecules,
            "reactions": self.reactions,
        }
        pickle_dump(d, filename)

    @classmethod
    def from_file(cls, filename):
        d = pickle_load(filename)
        logger.info(
            "{} reactions loaded from file: {}".format(len(d["reactions"]), filename)
        )
        return cls(d["molecules"], d["reactions"])


def is_valid_A_to_B_reaction(reactant, product, first_only=True):
    """
    Check whether the reactant and product can form A -> B style reaction w.r.t.
    isomorphism.

    Args:
        reactant, product (MoleculeWrapper): molecules
        first_only (bool): If `True`, only return the first found one. If `False`
            return all.

    Returns:
        list: bonds of reactant (represented by a tuple of the two atoms associated
            with the bond) by breaking which A -> B reaction is valid.
            Could be empty if no such reaction can form.
    """
    bonds = []
    for b, mgs in reactant.fragments.items():
        if len(mgs) == 1 and mgs[0].isomorphic_to(product.mol_graph):
            bonds.append(b)
            if first_only:
                return bonds
    return bonds


def is_valid_A_to_B_C_reaction(reactant, product1, product2, first_only=True):
    """
    Check whether the reactant and product can form A -> B + C style reaction w.r.t.
    isomorphism.

    Args:
        reactant, product1, product2 (MoleculeWrapper): molecules
        first_only (bool): If `True`, only return the first found one. If `False`
            return all.

    Returns:
        list: bonds of reactant (represented by a tuple of the two atoms associated
            with the bond) by breaking which A -> B + C reaction is valid.
            Could be empty if no such reaction can form.
    """

    bonds = []
    for b, mgs in reactant.fragments.items():
        if len(mgs) == 2:
            if (
                mgs[0].isomorphic_to(product1.mol_graph)
                and mgs[1].isomorphic_to(product2.mol_graph)
            ) or (
                mgs[0].isomorphic_to(product2.mol_graph)
                and mgs[1].isomorphic_to(product1.mol_graph)
            ):
                bonds.append(b)
                if first_only:
                    return bonds
    return bonds


def nx_graph_atom_mapping(g1, g2):
    """
    Mapping the atoms from g1 to g2 based on isomorphism.

    Args:
        g1, g2: nx graph

    Returns:
        dict: atom mapping from g1 to g2, but `None` is g1 is not isomorphic to g2.

    See Also:
        https://networkx.github.io/documentation/stable/reference/algorithms/isomorphism.vf2.html
    """
    nm = iso.categorical_node_match("specie", "ERROR")
    GM = iso.GraphMatcher(g1.to_undirected(), g2.to_undirected(), node_match=nm)
    if GM.is_isomorphic():
        return GM.mapping
    else:
        return None


def get_same_bond_breaking_reactions_between_two_reaction_groups(
    reactant1, group1, reactant2, group2
):
    """
    Args:
        reactant1 (MolecularWrapper instance)
        group1, (dict): A group of reactions that have the same reactant1 but
            breaking different bonds. the bond indices is the key of the dict.
        reactant2 (MolecularWrapper instance) reactant2 should have the same
            isomorphism as that of reactant1, but other property can be different,
            e.g. (charge).
        group2 (dict): A group of reactions that have the same reactant2 but
            breaking different bonds. the bond indices is the key of the dict.

    Returns:
        A list of tuples (rxn1, rxn2) where rxn1 and rxn2 has the same breaking bond.
    """

    bonds1 = [tuple(k) for k in group1]
    bonds2 = [tuple(k) for k in group2]
    fragments1 = reactant1.fragments[bonds1]
    fragments2 = reactant2.fragments[bonds2]

    res = []
    for b1, mgs1 in fragments1.items():
        for b2, mgs2 in fragments2.items():
            if len(mgs1) == len(mgs2) == 1:
                if mgs1[0].isomorphic_to(mgs2[0]):
                    res.append((group1[b1], group2[b2]))

            if len(mgs1) == len(mgs2) == 2:
                if (
                    mgs1[0].isomorphic_to(mgs2[0]) and mgs1[1].isomorphic_to(mgs2[1])
                ) or (mgs1[0].isomorphic_to(mgs2[1]) and mgs1[1].isomorphic_to(mgs2[0])):
                    res.append((group1[b1], group2[b2]))
    return res
