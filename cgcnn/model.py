from __future__ import division, print_function

import torch
import torch.nn as nn


class ConvLayer(nn.Module):
    """
    Convolutional operation on graphs
    """

    def __init__(self, atom_embedding_size, nbr_fea_len):
        """
        Initialize ConvLayer.

        Parameters
        ----------

        atom_embedding_size: int
          Number of atom hidden features.
        nbr_fea_len: int
          Number of bond features.
        """
        super(ConvLayer, self).__init__()
        self.atom_embedding_size = atom_embedding_size
        self.nbr_fea_len = nbr_fea_len
        self.fc_full = nn.Linear(
            2 * self.atom_embedding_size + self.nbr_fea_len,
            2 * self.atom_embedding_size,
        )
        self.sigmoid = nn.Sigmoid()
        self.softplus1 = nn.Softplus()
        self.bn1 = nn.BatchNorm1d(2 * self.atom_embedding_size)
        self.bn2 = nn.BatchNorm1d(self.atom_embedding_size)
        self.softplus2 = nn.Softplus()

    def forward(self, atom_in_fea, nbr_fea, nbr_fea_idx):
        """
        Forward pass

        N: Total number of atoms in the batch
        M: Max number of neighbors

        Parameters
        ----------

        atom_in_fea: Variable(torch.Tensor) shape (N, atom_embedding_size)
          Atom hidden features before convolution
        nbr_fea: Variable(torch.Tensor) shape (N, M, nbr_fea_len)
          Bond features of each atom's M neighbors
        nbr_fea_idx: torch.LongTensor shape (N, M)
          Indices of M neighbors of each atom

        Returns
        -------

        atom_out_fea: nn.Variable shape (N, atom_embedding_size)
          Atom hidden features after convolution

        """
        # TODO will there be problems with the index zero padding?
        N, M = nbr_fea_idx.shape
        # convolution
        atom_nbr_fea = atom_in_fea[nbr_fea_idx, :]
        total_nbr_fea = torch.cat(
            [
                atom_in_fea.unsqueeze(1).expand(
                    N, M, self.atom_embedding_size
                ),
                atom_nbr_fea,
                nbr_fea,
            ],
            dim=2,
        )
        total_gated_fea = self.fc_full(total_nbr_fea)
        total_gated_fea = self.bn1(
            total_gated_fea.view(-1, self.atom_embedding_size * 2)
        ).view(N, M, self.atom_embedding_size * 2)
        nbr_filter, nbr_core = total_gated_fea.chunk(2, dim=2)
        nbr_filter = self.sigmoid(nbr_filter)
        nbr_core = self.softplus1(nbr_core)
        nbr_sumed = torch.sum(nbr_filter * nbr_core, dim=1)
        nbr_sumed = self.bn2(nbr_sumed)
        out = self.softplus2(atom_in_fea + nbr_sumed)
        return out


class CrystalGraphConvNet(nn.Module):
    """
    Create a crystal graph convolutional neural network for predicting total
    material properties.
    """

    def __init__(
        self,
        orig_atom_fea_len,
        nbr_fea_len,
        atom_embedding_size=64,
        num_graph_conv_layers=3,
        fc_feat_size=128,
        num_fc_layers=1,
        classification=False,
    ):
        """
        Initialize CrystalGraphConvNet.

        Parameters
        ----------

        orig_atom_fea_len: int
          Number of atom features in the input.
        nbr_fea_len: int
          Number of bond features.
        atom_embedding_size: int
          Number of hidden atom features in the convolutional layers
        num_graph_conv_layers: int
          Number of convolutional layers
        fc_feat_size: int
          Number of hidden features after pooling
        num_fc_layers: int
          Number of hidden layers after pooling
        """
        super(CrystalGraphConvNet, self).__init__()
        self.classification = classification
        self.embedding = nn.Linear(orig_atom_fea_len, atom_embedding_size)
        self.convs = nn.ModuleList(
            [
                ConvLayer(
                    atom_embedding_size=atom_embedding_size,
                    nbr_fea_len=nbr_fea_len,
                )
                for _ in range(num_graph_conv_layers)
            ]
        )
        self.conv_to_fc = nn.Linear(atom_embedding_size, fc_feat_size)
        self.conv_to_fc_softplus = nn.Softplus()
        if num_fc_layers > 1:
            self.fcs = nn.ModuleList(
                [
                    nn.Linear(fc_feat_size, fc_feat_size)
                    for _ in range(num_fc_layers - 1)
                ]
            )
            self.softpluses = nn.ModuleList(
                [nn.Softplus() for _ in range(num_fc_layers - 1)]
            )
            # self.bn = nn.ModuleList([nn.BatchNorm1d(fc_feat_size)
            #                                 for _ in range(num_fc_layers-1)])
        if self.classification:
            self.fc_out = nn.Linear(fc_feat_size, 2)
        else:
            self.fc_out = nn.Linear(fc_feat_size, 1)
        if self.classification:
            self.logsoftmax = nn.LogSoftmax()
            self.dropout = nn.Dropout()

        self.to("cuda", non_blocking=True)
        for module in self.convs:
            module.to("cuda", non_blocking=True)

    def forward(self, atom_fea, nbr_fea, nbr_fea_idx, crystal_atom_idx):
        """
        Forward pass

        N: Total number of atoms in the batch
        M: Max number of neighbors
        N0: Total number of crystals in the batch

        Parameters
        ----------

        atom_fea: Variable(torch.Tensor) shape (N, orig_atom_fea_len)
          Atom features from atom type
        nbr_fea: Variable(torch.Tensor) shape (N, M, nbr_fea_len)
          Bond features of each atom's M neighbors
        nbr_fea_idx: torch.LongTensor shape (N, M)
          Indices of M neighbors of each atom
        crystal_atom_idx: list of torch.LongTensor of length N0
          Mapping from the crystal idx to atom idx

        Returns
        -------

        prediction: nn.Variable shape (N, )
          Atom hidden features after convolution

        """
        atom_fea = self.embedding(atom_fea)
        for conv_func in self.convs:
            atom_fea = conv_func(atom_fea, nbr_fea, nbr_fea_idx)
        crys_fea = self.pooling(atom_fea, crystal_atom_idx)
        crys_fea = self.conv_to_fc(self.conv_to_fc_softplus(crys_fea))
        crys_fea = self.conv_to_fc_softplus(crys_fea)
        if self.classification:
            crys_fea = self.dropout(crys_fea)
        if hasattr(self, "fcs") and hasattr(self, "softpluses"):
            for fc, softplus in zip(self.fcs, self.softpluses):
                crys_fea = softplus(fc(crys_fea))
        out = self.fc_out(crys_fea)
        if self.classification:
            out = self.logsoftmax(out)
        return out

    def pooling(self, atom_fea, crystal_atom_idx):
        """
        Pooling the atom features to crystal features

        N: Total number of atoms in the batch
        N0: Total number of crystals in the batch

        Parameters
        ----------

        atom_fea: Variable(torch.Tensor) shape (N, atom_fea_len)
          Atom feature vectors of the batch
        crystal_atom_idx: list of torch.LongTensor of length N0
          Mapping from the crystal idx to atom idx
        """
        assert (
            sum([len(idx_map) for idx_map in crystal_atom_idx])
            == atom_fea.data.shape[0]
        )
        summed_fea = [
            torch.mean(atom_fea[idx_map], dim=0, keepdim=True)
            for idx_map in crystal_atom_idx
        ]
        return torch.cat(summed_fea, dim=0)
